from typing_extensions import override
import asyncio
import threading
import time
import requests
import os
import sys
import json
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional, List

# Set up deps path before importing twitchio
current_dir = os.path.dirname(os.path.abspath(__file__))
deps_path = os.path.join(current_dir, 'deps')
if deps_path not in sys.path:
    sys.path.insert(0, deps_path)

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from lib.PluginHelper import PluginHelper, Projection
from lib.PluginSettingDefinitions import PluginSettings, SettingsGrid, TextSetting, ToggleSetting
from lib.Logger import log
from lib.PluginBase import PluginBase, PluginManifest
from lib.Event import PluginEvent, Event

# ============================================================================
# GENUI PROJECTION
# Exposes live Twitch chat to the GenUI overlay system.
# Updated directly from the bot — no game events needed.
# Ask COVAS to "show Twitch chat on the HUD" to render it.
# ============================================================================

class TwitchChatMessage(BaseModel):
    author: str = Field(default="", description="Chatter's display name")
    content: str = Field(default="", description="Message text")
    time: str = Field(default="", description="Timestamp HH:MM")
    is_mention: bool = Field(default=False, description="True if this message triggered a mention event")

class TwitchAlertEntry(BaseModel):
    type: str = Field(default="", description="Alert type: follow/sub/resub/giftsub/bits/raid/redeem")
    user: str = Field(default="", description="Username who triggered the alert")
    detail: str = Field(default="", description="Additional detail e.g. tier, amount, viewer count")
    time: str = Field(default="", description="Timestamp HH:MM")

class TwitchChatStateModel(BaseModel):
    connected: bool = Field(default=False, description="Whether the bot is connected to Twitch")
    channel: str = Field(default="", description="Channel name the bot is connected to")
    messages: List[TwitchChatMessage] = Field(default_factory=list, description="Recent chat messages (up to 15)")
    last_alert: Optional[TwitchAlertEntry] = Field(default=None, description="Most recent channel alert")

class TwitchChatProjection(Projection[TwitchChatStateModel]):
    StateModel = TwitchChatStateModel

    def process(self, event: Event) -> None:
        pass  # State is updated directly by CovasCast, not from game events

# ============================================================================
# PARAM MODELS
# ============================================================================

class EmptyParams(BaseModel):
    pass

class ChatStatusParams(BaseModel):
    limit: Optional[int] = 5

class SendChatParams(BaseModel):
    message: str

class TimeoutParams(BaseModel):
    username: str
    duration: Optional[int] = 60
    reason: Optional[str] = None

class BanParams(BaseModel):
    username: str
    reason: Optional[str] = None

class UnbanParams(BaseModel):
    username: str

class DeleteMessageParams(BaseModel):
    message_id: str

# ============================================================================
# RATE LIMITER
# ============================================================================

class RateLimiter:
    def __init__(self, interval_seconds: float = 10.0):
        self.interval = interval_seconds
        self.last_allowed = 0.0
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = time.time()
            if now - self.last_allowed >= self.interval:
                self.last_allowed = now
                return True
            return False

# ============================================================================
# TWITCHIO 3.x BOT
# Uses EventSub WebSocket — no public server required.
# Tokens are passed directly, bypassing the OAuth web flow.
# ============================================================================

class TwitchBot(commands.Bot):
    def __init__(self, plugin_instance, client_id: str, client_secret: str,
                 bot_id: str, broadcaster_id: str,
                 bot_access_token: str, bot_refresh_token: str,
                 broadcaster_access_token: str, broadcaster_refresh_token: str,
                 channel: str):
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            bot_id=bot_id,
            owner_id=broadcaster_id,
            prefix='!',
        )
        self.plugin = plugin_instance
        self.channel_name = channel.lower().lstrip('#')
        self._bot_access_token = bot_access_token
        self._bot_refresh_token = bot_refresh_token
        self._broadcaster_access_token = broadcaster_access_token
        self._broadcaster_refresh_token = broadcaster_refresh_token
        self._bot_id = bot_id
        self._broadcaster_id = broadcaster_id

    async def setup_hook(self) -> None:
        """Called after login — add tokens and subscribe to EventSub.
        Each subscription is wrapped individually so one missing scope
        doesn't prevent the others from registering."""
        await self.add_token(self._bot_access_token, self._bot_refresh_token)
        await self.add_token(self._broadcaster_access_token, self._broadcaster_refresh_token)

        for name, payload_fn, token_for in [
            ('chat',       lambda: eventsub.ChatMessageSubscription(broadcaster_user_id=self._broadcaster_id, user_id=self._bot_id), self._broadcaster_id),
            ('follow',     lambda: eventsub.ChannelFollowSubscription(broadcaster_user_id=self._broadcaster_id, moderator_user_id=self._bot_id), self._bot_id),
            ('subscribe',  lambda: eventsub.ChannelSubscribeSubscription(broadcaster_user_id=self._broadcaster_id), self._broadcaster_id),
            ('resub',      lambda: eventsub.ChannelSubscribeMessageSubscription(broadcaster_user_id=self._broadcaster_id), self._broadcaster_id),
            ('giftsub',    lambda: eventsub.ChannelSubscriptionGiftSubscription(broadcaster_user_id=self._broadcaster_id), self._broadcaster_id),
            ('cheer',      lambda: eventsub.ChannelCheerSubscription(broadcaster_user_id=self._broadcaster_id), self._broadcaster_id),
            ('raid',       lambda: eventsub.ChannelRaidSubscription(to_broadcaster_user_id=self._broadcaster_id), self._broadcaster_id),
            ('redemption', lambda: eventsub.ChannelPointsRedeemAddSubscription(broadcaster_user_id=self._broadcaster_id), self._broadcaster_id),
        ]:
            try:
                await self.subscribe_websocket(payload=payload_fn(), token_for=token_for)
                log('info', f'COVASCAST: Subscribed to {name}')
            except Exception as e:
                log('info', f'COVASCAST: Failed to subscribe to {name}: {str(e)}')

    async def event_ready(self) -> None:
        log('info', f'COVASCAST: Connected as bot_id={self._bot_id}')
        self.plugin.connected = True
        self.plugin.chat_projection.state.connected = True
        self.plugin.chat_projection.state.channel = self.channel_name

    # -------------------------------------------------------------------------
    # CHAT MESSAGES
    # -------------------------------------------------------------------------

    @commands.Component.listener()
    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        author = payload.chatter.name if payload.chatter else 'unknown'
        content = payload.text or ''
        mention_trigger = self.plugin.mention_trigger.lower()

        log('info', f'COVASCAST: CHAT - {author}: {content}')

        # OpenAI moderation check — run first before adding to any cache
        if self.plugin.moderation_enabled and self.plugin.openai_api_key:
            flagged, categories = self.plugin._check_moderation(content)
            if flagged:
                log('info', f'COVASCAST: Message from {author} flagged by moderation')
                if self.plugin.moderation_announce and self.plugin.helper:
                    try:
                        flagged_cats = [c for c, v in categories.items() if v]
                        self.plugin.helper.dispatch_event(PluginEvent(
                            plugin_event_name='twitch_moderated',
                            plugin_event_content={
                                'author': author,
                                'categories': ', '.join(flagged_cats) if flagged_cats else 'policy violation'
                            }
                        ))
                    except Exception as e:
                        log('info', f'COVASCAST: moderation dispatch failed: {str(e)}')
                return

        # Update GenUI projection
        is_mention = bool(mention_trigger and mention_trigger in content.lower())
        msg = TwitchChatMessage(
            author=author,
            content=content,
            time=datetime.now().strftime('%H:%M'),
            is_mention=is_mention
        )
        self.plugin.chat_projection.state.messages.append(msg)
        if len(self.plugin.chat_projection.state.messages) > 15:
            self.plugin.chat_projection.state.messages.pop(0)

        # Update local cache
        self.plugin.recent_chat.append({
            'author': author,
            'content': content,
            'timestamp': datetime.now().isoformat()
        })
        if len(self.plugin.recent_chat) > 100:
            self.plugin.recent_chat.pop(0)

        # Persist last 10 messages for context on restart
        self.plugin._save_recent_chat()

        # Mention — trigger immediate reply
        if is_mention:
            log('info', f'COVASCAST: Mention detected from {author}')
            self.plugin.recent_mentions.append({
                'author': author,
                'content': content,
                'timestamp': datetime.now().isoformat()
            })
            if len(self.plugin.recent_mentions) > 20:
                self.plugin.recent_mentions.pop(0)

            if self.plugin.helper:
                try:
                    self.plugin.helper.dispatch_event(PluginEvent(
                        plugin_event_name='twitch_mention',
                        plugin_event_content={'author': author, 'message': content}
                    ))
                except Exception as e:
                    log('info', f'COVASCAST: dispatch_event failed: {str(e)}')
        else:
            # Background chat — rate limited
            if self.plugin.chat_rate_limiter.allow() and self.plugin.helper:
                try:
                    self.plugin.helper.dispatch_event(PluginEvent(
                        plugin_event_name='twitch_chat',
                        plugin_event_content={'author': author, 'message': content}
                    ))
                except Exception as e:
                    log('info', f'COVASCAST: dispatch_event failed: {str(e)}')

    # -------------------------------------------------------------------------
    # CHANNEL ALERTS
    # -------------------------------------------------------------------------

    @commands.Component.listener()
    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:
        user = payload.user.name if payload.user else 'Someone'
        log('info', f'COVASCAST: Follow from {user}')
        self.plugin._fire_alert('follow', user=user)

    @commands.Component.listener()
    async def event_subscribe(self, payload: twitchio.ChannelSubscribe) -> None:
        user = payload.user.name if payload.user else 'Someone'
        tier = getattr(payload, 'tier', '1000')
        tier_name = {'1000': 'Tier 1', '2000': 'Tier 2', '3000': 'Tier 3'}.get(str(tier), 'Tier 1')
        log('info', f'COVASCAST: Sub from {user} ({tier_name})')
        self.plugin._fire_alert('sub', user=user, tier=tier_name)

    @commands.Component.listener()
    async def event_subscription_message(self, payload: twitchio.ChannelSubscriptionMessage) -> None:
        user = payload.user.name if payload.user else 'Someone'
        months = getattr(payload, 'cumulative_months', 1)
        message = payload.message.text if payload.message else ''
        log('info', f'COVASCAST: Resub from {user} ({months} months)')
        self.plugin._fire_alert('resub', user=user, months=months, message=message)

    @commands.Component.listener()
    async def event_subscription_gift(self, payload: twitchio.ChannelSubscriptionGift) -> None:
        user = payload.user.name if payload.user else 'Anonymous'
        total = getattr(payload, 'total', 1)
        log('info', f'COVASCAST: Gift sub from {user} (x{total})')
        self.plugin._fire_alert('giftsub', user=user, total=total)

    @commands.Component.listener()
    async def event_cheer(self, payload: twitchio.ChannelCheer) -> None:
        user = payload.user.name if payload.user else 'Anonymous'
        bits = payload.bits
        message = payload.message or ''
        log('info', f'COVASCAST: {bits} bits from {user}')
        self.plugin._fire_alert('bits', user=user, amount=bits, message=message)

    @commands.Component.listener()
    async def event_raid(self, payload: twitchio.ChannelRaid) -> None:
        raider = payload.raider.name if payload.raider else 'Someone'
        viewers = payload.viewers
        log('info', f'COVASCAST: Raid from {raider} with {viewers} viewers')
        self.plugin._fire_alert('raid', user=raider, viewers=viewers)

    @commands.Component.listener()
    async def event_channel_points_redeem_add(self, payload: twitchio.ChannelPointsRedemptionAdd) -> None:
        user = payload.user.name if payload.user else 'Someone'
        reward = payload.reward.title if payload.reward else 'a reward'
        log('info', f'COVASCAST: {user} redeemed {reward}')
        self.plugin._fire_alert('redeem', user=user, reward=reward)


# ============================================================================
# MAIN PLUGIN CLASS
# ============================================================================

class CovasCastPlugin(PluginBase):

    def __init__(self, plugin_manifest: PluginManifest):
        super().__init__(plugin_manifest)

        self.bot = None
        self.bot_thread = None
        self.bot_loop = None
        self.connected = False
        self.helper = None

        # Local state cache
        self.recent_chat = []
        self.recent_mentions = []
        self.last_alert = None

        # Rate limiter — 1 background chat event per 10 seconds
        self.chat_rate_limiter = RateLimiter(interval_seconds=10.0)

        # GenUI projection
        self.chat_projection = TwitchChatProjection()

        # Settings cache
        self.settings = {}
        self.channel = ''
        self.mention_trigger = '@covas'
        self.moderation_enabled = False
        self.moderation_announce = False
        self.moderation_categories = set()
        self.openai_api_key = ''

        # Bot capability flags
        self.allow_post_chat = False
        self.allow_delete_messages = False
        self.allow_timeout = False
        self.allow_ban = False
        self.allow_unban = False

    settings_config = PluginSettings(
        key="CovasCastPlugin",
        label="CovasCast",
        icon="live_tv",
        grids=[
            SettingsGrid(
                key="twitch_app",
                label="Twitch Application (dev.twitch.tv)",
                fields=[
                    TextSetting(
                        key="client_id",
                        label="Client ID",
                        type="text",
                        readonly=False,
                        placeholder="From Twitch Developer Console",
                        default_value=""
                    ),
                    TextSetting(
                        key="client_secret",
                        label="Client Secret",
                        type="text",
                        readonly=False,
                        placeholder="From Twitch Developer Console",
                        default_value=""
                    ),
                ]
            ),
            SettingsGrid(
                key="twitch_bot",
                label="Bot Account",
                fields=[
                    TextSetting(
                        key="bot_id",
                        label="Bot User ID (numeric)",
                        type="text",
                        readonly=False,
                        placeholder="e.g. 123456789",
                        default_value=""
                    ),
                    TextSetting(
                        key="bot_access_token",
                        label="Bot Access Token",
                        type="text",
                        readonly=False,
                        placeholder="From TwitchTokenGenerator (no oauth: prefix)",
                        default_value=""
                    ),
                    TextSetting(
                        key="bot_refresh_token",
                        label="Bot Refresh Token",
                        type="text",
                        readonly=False,
                        placeholder="From TwitchTokenGenerator",
                        default_value=""
                    ),
                ]
            ),
            SettingsGrid(
                key="twitch_broadcaster",
                label="Broadcaster Account",
                fields=[
                    TextSetting(
                        key="channel",
                        label="Channel Name",
                        type="text",
                        readonly=False,
                        placeholder="your_channel_name (no #)",
                        default_value=""
                    ),
                    TextSetting(
                        key="broadcaster_id",
                        label="Broadcaster User ID (numeric)",
                        type="text",
                        readonly=False,
                        placeholder="e.g. 987654321",
                        default_value=""
                    ),
                    TextSetting(
                        key="broadcaster_access_token",
                        label="Broadcaster Access Token",
                        type="text",
                        readonly=False,
                        placeholder="From TwitchTokenGenerator (no oauth: prefix)",
                        default_value=""
                    ),
                    TextSetting(
                        key="broadcaster_refresh_token",
                        label="Broadcaster Refresh Token",
                        type="text",
                        readonly=False,
                        placeholder="From TwitchTokenGenerator",
                        default_value=""
                    ),
                    TextSetting(
                        key="mention_trigger",
                        label="Mention Trigger",
                        type="text",
                        readonly=False,
                        placeholder="@covas",
                        default_value="@covas"
                    ),
                ]
            ),
            SettingsGrid(
                key="bot_capabilities",
                label="Bot Capabilities",
                fields=[
                    ToggleSetting(
                        key="allow_post_chat",
                        label="Allow: Post messages to chat",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="allow_delete_messages",
                        label="Allow: Delete messages (requires mod)",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="allow_timeout",
                        label="Allow: Timeout users (requires mod)",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="allow_ban",
                        label="Allow: Ban users (requires mod)",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="allow_unban",
                        label="Allow: Unban / untimeout users (requires mod)",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                ]
            ),
            SettingsGrid(
                key="moderation_settings",
                label="OpenAI Moderation (Optional)",
                fields=[
                    ToggleSetting(
                        key="moderation_enabled",
                        label="Enable OpenAI Content Moderation",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="moderation_announce",
                        label="Announce filtered messages (off = silent drop)",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    TextSetting(
                        key="openai_api_key",
                        label="OpenAI API Key",
                        type="text",
                        readonly=False,
                        placeholder="sk-...",
                        default_value=""
                    ),
                    ToggleSetting(
                        key="filter_harassment",
                        label="Filter: Harassment",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="filter_harassment_threatening",
                        label="Filter: Harassment / Threatening",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="filter_hate",
                        label="Filter: Hate",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=True
                    ),
                    ToggleSetting(
                        key="filter_hate_threatening",
                        label="Filter: Hate / Threatening",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=True
                    ),
                    ToggleSetting(
                        key="filter_sexual",
                        label="Filter: Sexual",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=True
                    ),
                    ToggleSetting(
                        key="filter_sexual_minors",
                        label="Filter: Sexual / Minors",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=True
                    ),
                    ToggleSetting(
                        key="filter_violence",
                        label="Filter: Violence",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="filter_violence_graphic",
                        label="Filter: Violence / Graphic",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="filter_self_harm",
                        label="Filter: Self-harm",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=True
                    ),
                    ToggleSetting(
                        key="filter_self_harm_intent",
                        label="Filter: Self-harm / Intent",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=True
                    ),
                    ToggleSetting(
                        key="filter_self_harm_instructions",
                        label="Filter: Self-harm / Instructions",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=True
                    ),
                    ToggleSetting(
                        key="filter_illicit",
                        label="Filter: Illicit",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                    ToggleSetting(
                        key="filter_illicit_violent",
                        label="Filter: Illicit / Violent",
                        type="toggle",
                        readonly=False,
                        placeholder=None,
                        default_value=False
                    ),
                ]
            )
        ]
    )

    @override
    def get_settings_config(self):
        return self.settings_config

    def on_settings_changed(self, settings: dict):
        self.settings = settings

    # -------------------------------------------------------------------------
    # LIFECYCLE
    # -------------------------------------------------------------------------

    @override
    def on_chat_start(self, helper: PluginHelper):
        self.helper = helper

        # Parse settings
        self.channel = self.settings.get('channel', '').strip().lstrip('#')
        self.mention_trigger = self.settings.get('mention_trigger', '@covas').strip()
        self.moderation_enabled = self.settings.get('moderation_enabled', False)
        self.moderation_announce = self.settings.get('moderation_announce', False)
        self.openai_api_key = self.settings.get('openai_api_key', '').strip()

        category_map = {
            'filter_harassment':             'harassment',
            'filter_harassment_threatening': 'harassment/threatening',
            'filter_hate':                   'hate',
            'filter_hate_threatening':       'hate/threatening',
            'filter_sexual':                 'sexual',
            'filter_sexual_minors':          'sexual/minors',
            'filter_violence':               'violence',
            'filter_violence_graphic':       'violence/graphic',
            'filter_self_harm':              'self-harm',
            'filter_self_harm_intent':       'self-harm/intent',
            'filter_self_harm_instructions': 'self-harm/instructions',
            'filter_illicit':                'illicit',
            'filter_illicit_violent':        'illicit/violent',
        }
        self.moderation_categories = {
            cat for key, cat in category_map.items()
            if self.settings.get(key, False)
        }

        self.allow_post_chat = self.settings.get('allow_post_chat', False)
        self.allow_delete_messages = self.settings.get('allow_delete_messages', False)
        self.allow_timeout = self.settings.get('allow_timeout', False)
        self.allow_ban = self.settings.get('allow_ban', False)
        self.allow_unban = self.settings.get('allow_unban', False)

        log('info', 'COVASCAST: Chat started')

        try:
            # Register GenUI projection
            helper.register_projection(self.chat_projection)
            log('info', 'COVASCAST: Chat projection registered')

            # Register events
            helper.register_event(
                name='twitch_mention',
                should_reply_check=lambda e: True,
                prompt_generator=self._mention_prompt
            )
            helper.register_event(
                name='twitch_alert',
                should_reply_check=lambda e: True,
                prompt_generator=self._alert_prompt
            )
            helper.register_event(
                name='twitch_chat',
                should_reply_check=lambda e: False,
                prompt_generator=self._chat_background_prompt
            )
            helper.register_event(
                name='twitch_moderated',
                should_reply_check=lambda e: True,
                prompt_generator=self._moderated_prompt
            )

            # Register tools
            helper.register_action(
                'twitch_status',
                "Get recent Twitch chat mentions and channel status on demand.",
                ChatStatusParams, self.twitch_status, 'global'
            )
            if self.allow_post_chat:
                helper.register_action(
                    'twitch_send_chat',
                    "Post a message to Twitch chat.",
                    SendChatParams, self.twitch_send_chat, 'global'
                )
            if self.allow_delete_messages:
                helper.register_action(
                    'twitch_delete_message',
                    "Delete a specific message from Twitch chat by message ID.",
                    DeleteMessageParams, self.twitch_delete_message, 'global'
                )
            if self.allow_timeout:
                helper.register_action(
                    'twitch_timeout',
                    "Timeout a Twitch user. Specify username, duration in seconds, and optional reason.",
                    TimeoutParams, self.twitch_timeout, 'global'
                )
            if self.allow_ban:
                helper.register_action(
                    'twitch_ban',
                    "Permanently ban a Twitch user. Specify username and optional reason.",
                    BanParams, self.twitch_ban, 'global'
                )
            if self.allow_unban:
                helper.register_action(
                    'twitch_unban',
                    "Unban or untimeout a Twitch user by username.",
                    UnbanParams, self.twitch_unban, 'global'
                )

            # Register status generator
            helper.register_status_generator(self.generate_twitch_status)

            # Start bot
            client_id = self.settings.get('client_id', '').strip()
            client_secret = self.settings.get('client_secret', '').strip()
            bot_id = self.settings.get('bot_id', '').strip()
            bot_access_token = self.settings.get('bot_access_token', '').strip()
            bot_refresh_token = self.settings.get('bot_refresh_token', '').strip()
            broadcaster_id = self.settings.get('broadcaster_id', '').strip()
            broadcaster_access_token = self.settings.get('broadcaster_access_token', '').strip()
            broadcaster_refresh_token = self.settings.get('broadcaster_refresh_token', '').strip()
            channel = self.channel

            if all([client_id, client_secret, bot_id, bot_access_token, bot_refresh_token,
                    broadcaster_id, broadcaster_access_token, broadcaster_refresh_token, channel]):
                self._start_bot(
                    client_id, client_secret,
                    bot_id, broadcaster_id,
                    bot_access_token, bot_refresh_token,
                    broadcaster_access_token, broadcaster_refresh_token,
                    channel
                )
            else:
                log('info', 'COVASCAST: Missing required settings — configure in plugin settings.')

            log('info', 'COVASCAST: Setup complete')

            # Load recent chat history for startup context
            self._load_recent_chat()

        except Exception as e:
            log('info', f'COVASCAST: Failed during chat start: {str(e)}')

    @override
    def on_chat_stop(self, helper: PluginHelper):
        log('info', 'COVASCAST: Chat stopped — disconnecting')
        self._stop_bot()
        self.helper = None
        self.chat_projection.state.connected = False

    # -------------------------------------------------------------------------
    # BOT THREAD MANAGEMENT
    # -------------------------------------------------------------------------

    def _start_bot(self, client_id, client_secret, bot_id, broadcaster_id,
                   bot_access_token, bot_refresh_token,
                   broadcaster_access_token, broadcaster_refresh_token, channel):
        try:
            self.bot_loop = asyncio.new_event_loop()

            self.bot = TwitchBot(
                plugin_instance=self,
                client_id=client_id,
                client_secret=client_secret,
                bot_id=bot_id,
                broadcaster_id=broadcaster_id,
                bot_access_token=bot_access_token,
                bot_refresh_token=bot_refresh_token,
                broadcaster_access_token=broadcaster_access_token,
                broadcaster_refresh_token=broadcaster_refresh_token,
                channel=channel
            )

            def run_bot():
                asyncio.set_event_loop(self.bot_loop)
                try:
                    self.bot_loop.run_until_complete(self.bot.start())
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log('info', f'COVASCAST: Bot error: {str(e)}')
                finally:
                    self.connected = False
                    self.chat_projection.state.connected = False
                    log('info', 'COVASCAST: Bot stopped')

            self.bot_thread = threading.Thread(target=run_bot, daemon=True)
            self.bot_thread.start()
            log('info', 'COVASCAST: Bot thread started')

        except Exception as e:
            log('info', f'COVASCAST: Failed to start bot: {str(e)}')

    def _stop_bot(self):
        try:
            if self.bot and self.bot_loop and not self.bot_loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(
                    self.bot.close(),
                    self.bot_loop
                )
                future.result(timeout=5)
        except Exception as e:
            log('info', f'COVASCAST: Error stopping bot: {str(e)}')
        finally:
            self.connected = False
            self.bot = None

    def _run_async(self, coro):
        if not self.bot_loop or self.bot_loop.is_closed():
            raise RuntimeError("Bot event loop is not running.")
        future = asyncio.run_coroutine_threadsafe(coro, self.bot_loop)
        return future.result(timeout=10)

    # -------------------------------------------------------------------------
    # ALERT DISPATCHER
    # -------------------------------------------------------------------------

    def _fire_alert(self, alert_type: str, **kwargs):
        self.last_alert = {
            'type': alert_type,
            'timestamp': datetime.now().isoformat(),
            **kwargs
        }

        # Update GenUI projection
        detail_map = {
            'sub': kwargs.get('tier', ''),
            'resub': f"{kwargs.get('months', '')} months",
            'giftsub': f"x{kwargs.get('total', 1)}",
            'bits': f"{kwargs.get('amount', 0)} bits",
            'raid': f"{kwargs.get('viewers', 0)} viewers",
            'redeem': kwargs.get('reward', ''),
        }
        self.chat_projection.state.last_alert = TwitchAlertEntry(
            type=alert_type,
            user=kwargs.get('user', 'Someone'),
            detail=detail_map.get(alert_type, ''),
            time=datetime.now().strftime('%H:%M')
        )

        if self.helper:
            self.helper.dispatch_event(PluginEvent(
                plugin_event_name='twitch_alert',
                plugin_event_content={'type': alert_type, **kwargs}
            ))

    # -------------------------------------------------------------------------
    # EVENT PROMPT GENERATORS
    # -------------------------------------------------------------------------

    def _mention_prompt(self, event: PluginEvent) -> str:
        author = event.plugin_event_content.get('author', 'Someone')
        message = event.plugin_event_content.get('message', '')
        chat_note = " You may also respond in chat using twitch_send_chat." if self.allow_post_chat else ""
        return (
            f"Twitch chatter {author} mentioned you in chat: \"{message}\". "
            f"Respond verbally to their message.{chat_note}"
        )

    def _alert_prompt(self, event: PluginEvent) -> str:
        content = event.plugin_event_content
        alert_type = content.get('type', '')
        user = content.get('user', 'Someone')

        prompts = {
            'follow': f"{user} just followed the channel! Welcome them warmly.",
            'sub': f"{user} just subscribed ({content.get('tier', 'Tier 1')})! Celebrate their subscription.",
            'resub': (
                f"{user} resubscribed for {content.get('months', 1)} months!"
                + (f" They said: \"{content.get('message')}\"" if content.get('message') else '')
                + " Acknowledge their loyalty."
            ),
            'giftsub': f"{user} gifted {content.get('total', 1)} subscription(s) to the community! Thank them.",
            'bits': (
                f"{user} cheered {content.get('amount', 0)} bits!"
                + (f" They said: \"{content.get('message')}\"" if content.get('message') else '')
                + " Thank them enthusiastically."
            ),
            'raid': f"{user} is raiding with {content.get('viewers', 0)} viewers! Welcome the raiding party.",
            'redeem': f"{user} redeemed \"{content.get('reward', 'a reward')}\". Acknowledge their redemption.",
        }
        return prompts.get(alert_type, f"A Twitch event occurred: {alert_type} from {user}.")

    def _chat_background_prompt(self, event: PluginEvent) -> str:
        author = event.plugin_event_content.get('author', 'unknown')
        message = event.plugin_event_content.get('message', '')
        return f"Twitch chat — {author}: {message}"

    def _moderated_prompt(self, event: PluginEvent) -> str:
        author = event.plugin_event_content.get('author', 'unknown')
        categories = event.plugin_event_content.get('categories', 'policy violation')
        return (
            f"A message from Twitch chatter {author} was filtered by content moderation "
            f"({categories}). Briefly acknowledge that a message was filtered if appropriate."
        )

    # -------------------------------------------------------------------------
    # STATUS GENERATOR
    # -------------------------------------------------------------------------

    def generate_twitch_status(self, projected_states: dict) -> list[tuple[str, str]]:
        try:
            if not self.connected:
                return [("Twitch", "Not connected")]

            parts = [f"Live on #{self.channel}"]

            if self.last_alert:
                alert_type = self.last_alert.get('type', '')
                user = self.last_alert.get('user', '')
                ts = self.last_alert.get('timestamp', '')
                parts.append(f"Last alert: {alert_type} from {user}")

            return [("Twitch", " | ".join(parts))]

        except Exception as e:
            log('info', f'COVASCAST: Status generator error: {str(e)}')
            return [("Twitch", "Connected")]

    # -------------------------------------------------------------------------
    # TOOLS
    # -------------------------------------------------------------------------

    def twitch_send_chat(self, args, projected_states) -> str:
        try:
            if not self.connected or not self.bot:
                return "COVASCAST: Not connected to Twitch."
            if not args.message:
                return "COVASCAST: No message provided."

            broadcaster_id = self.settings.get('broadcaster_id', '').strip()
            bot_id = self.settings.get('bot_id', '').strip()

            async def send():
                broadcaster = self.bot.create_partialuser(
                    user_id=int(broadcaster_id),
                    user_login=self.channel
                )
                await broadcaster.send_message(
                    sender=self.bot.create_partialuser(user_id=int(bot_id)),
                    message=args.message
                )

            self._run_async(send())
            log('info', f'COVASCAST: Sent to chat: {args.message[:50]}')
            return f"COVASCAST: Sent to chat: {args.message}"

        except Exception as e:
            log('info', f'COVASCAST: Send chat failed: {str(e)}')
            return f"COVASCAST: Failed to send message — {str(e)}"

    def twitch_status(self, args, projected_states) -> str:
        try:
            if not self.connected:
                return "COVASCAST: Not connected to Twitch."

            limit = min(args.limit or 5, 20)
            lines = [f"COVASCAST: Channel #{self.channel}"]

            if self.recent_mentions:
                recent = self.recent_mentions[-limit:]
                lines.append(f"\nRecent mentions ({len(recent)}):")
                for m in recent:
                    lines.append(f"  {m['author']}: {m['content']}")
            else:
                lines.append("\nNo recent mentions.")

            if self.last_alert:
                alert_type = self.last_alert.get('type', '')
                user = self.last_alert.get('user', '')
                lines.append(f"\nLast alert: {alert_type} from {user}")

            return "\n".join(lines)

        except Exception as e:
            log('info', f'COVASCAST: Status check failed: {str(e)}')
            return f"COVASCAST: Failed to get status — {str(e)}"

    def twitch_delete_message(self, args, projected_states) -> str:
        try:
            if not self.connected or not self.bot:
                return "COVASCAST: Not connected to Twitch."
            if not self.allow_delete_messages:
                return "COVASCAST: Delete messages not enabled in settings."

            broadcaster_id = self.settings.get('broadcaster_id', '').strip()
            bot_id = self.settings.get('bot_id', '').strip()

            async def delete():
                broadcaster = self.bot.create_partialuser(
                    user_id=int(broadcaster_id),
                    user_login=self.channel
                )
                moderator = self.bot.create_partialuser(user_id=int(bot_id))
                await broadcaster.delete_chat_messages(
                    moderator=moderator,
                    message_id=args.message_id
                )

            self._run_async(delete())
            log('info', f'COVASCAST: Deleted message {args.message_id}')
            return "COVASCAST: Message deleted."

        except Exception as e:
            log('info', f'COVASCAST: Delete message failed: {str(e)}')
            return f"COVASCAST: Failed to delete message — {str(e)}"

    def twitch_timeout(self, args, projected_states) -> str:
        try:
            if not self.connected or not self.bot:
                return "COVASCAST: Not connected to Twitch."
            if not self.allow_timeout:
                return "COVASCAST: Timeout not enabled in settings."

            username = args.username.strip().lstrip('@')
            duration = max(1, min(args.duration or 60, 1209600))
            reason = args.reason or ''
            broadcaster_id = self.settings.get('broadcaster_id', '').strip()
            bot_id = self.settings.get('bot_id', '').strip()

            async def timeout():
                users = await self.bot.fetch_users(logins=[username])
                if not users:
                    raise Exception(f"Could not find user: {username}")
                broadcaster = self.bot.create_partialuser(
                    user_id=int(broadcaster_id),
                    user_login=self.channel
                )
                moderator = self.bot.create_partialuser(user_id=int(bot_id))
                await broadcaster.timeout_user(
                    moderator=moderator,
                    user=users[0],
                    duration=duration,
                    reason=reason or None
                )

            self._run_async(timeout())
            log('info', f'COVASCAST: Timed out {username} for {duration}s')
            return f"COVASCAST: {username} timed out for {duration} seconds."

        except Exception as e:
            log('info', f'COVASCAST: Timeout failed: {str(e)}')
            return f"COVASCAST: Failed to timeout {args.username} — {str(e)}"

    def twitch_ban(self, args, projected_states) -> str:
        try:
            if not self.connected or not self.bot:
                return "COVASCAST: Not connected to Twitch."
            if not self.allow_ban:
                return "COVASCAST: Ban not enabled in settings."

            username = args.username.strip().lstrip('@')
            reason = args.reason or ''
            broadcaster_id = self.settings.get('broadcaster_id', '').strip()
            bot_id = self.settings.get('bot_id', '').strip()

            async def ban():
                users = await self.bot.fetch_users(logins=[username])
                if not users:
                    raise Exception(f"Could not find user: {username}")
                broadcaster = self.bot.create_partialuser(
                    user_id=int(broadcaster_id),
                    user_login=self.channel
                )
                moderator = self.bot.create_partialuser(user_id=int(bot_id))
                await broadcaster.ban_user(
                    moderator=moderator,
                    user=users[0],
                    reason=reason or None
                )

            self._run_async(ban())
            log('info', f'COVASCAST: Banned {username}')
            return f"COVASCAST: {username} has been banned."

        except Exception as e:
            log('info', f'COVASCAST: Ban failed: {str(e)}')
            return f"COVASCAST: Failed to ban {args.username} — {str(e)}"

    def twitch_unban(self, args, projected_states) -> str:
        try:
            if not self.connected or not self.bot:
                return "COVASCAST: Not connected to Twitch."
            if not self.allow_unban:
                return "COVASCAST: Unban not enabled in settings."

            username = args.username.strip().lstrip('@')
            broadcaster_id = self.settings.get('broadcaster_id', '').strip()
            bot_id = self.settings.get('bot_id', '').strip()

            async def unban():
                users = await self.bot.fetch_users(logins=[username])
                if not users:
                    raise Exception(f"Could not find user: {username}")
                broadcaster = self.bot.create_partialuser(
                    user_id=int(broadcaster_id),
                    user_login=self.channel
                )
                moderator = self.bot.create_partialuser(user_id=int(bot_id))
                await broadcaster.unban_user(
                    moderator=moderator,
                    user=users[0]
                )

            self._run_async(unban())
            log('info', f'COVASCAST: Unbanned {username}')
            return f"COVASCAST: {username} has been unbanned."

        except Exception as e:
            log('info', f'COVASCAST: Unban failed: {str(e)}')
            return f"COVASCAST: Failed to unban {args.username} — {str(e)}"

    # -------------------------------------------------------------------------
    # CHAT HISTORY PERSISTENCE
    # Saves last 10 messages to disk so startup context is available
    # -------------------------------------------------------------------------

    def _chat_history_path(self) -> str:
        return os.path.join(current_dir, 'chat_history.json')

    def _save_recent_chat(self):
        try:
            last_10 = self.recent_chat[-10:]
            with open(self._chat_history_path(), 'w', encoding='utf-8') as f:
                json.dump(last_10, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_recent_chat(self):
        try:
            path = self._chat_history_path()
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                if history:
                    self.recent_chat = history
                    # Also push to projection so HUD shows recent history
                    for msg in history[-15:]:
                        self.chat_projection.state.messages.append(TwitchChatMessage(
                            author=msg.get('author', ''),
                            content=msg.get('content', ''),
                            time=msg.get('timestamp', '')[:16].replace('T', ' '),
                            is_mention=False
                        ))
                    log('info', f'COVASCAST: Loaded {len(history)} messages from chat history')
        except Exception as e:
            log('info', f'COVASCAST: Could not load chat history: {str(e)}')

    # -------------------------------------------------------------------------
    # OPENAI MODERATION
    # -------------------------------------------------------------------------

    def _check_moderation(self, text: str) -> tuple:
        if not self.openai_api_key:
            return False, {}
        try:
            response = requests.post(
                "https://api.openai.com/v1/moderations",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.openai_api_key}"
                },
                json={"input": text},
                timeout=5
            )
            if response.status_code == 200:
                result = response.json()["results"][0]
                categories = result["categories"]
                flagged_cats = {c for c, v in categories.items() if v}

                if not self.moderation_categories:
                    return False, {}

                flagged_cats = flagged_cats & self.moderation_categories
                is_flagged = len(flagged_cats) > 0

                if is_flagged:
                    log('info', f'COVASCAST: Message flagged — {", ".join(flagged_cats)}')

                return is_flagged, {c: (c in flagged_cats) for c in categories}
            return False, {}
        except Exception as e:
            log('info', f'COVASCAST: Moderation check failed: {str(e)}')
            return False, {}

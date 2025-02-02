"""MediaPlayer platform for Music Assistant integration."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
)
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.components.media_player.const import (
    ATTR_MEDIA_ANNOUNCE,
    ATTR_MEDIA_ENQUEUE,
    ATTR_MEDIA_EXTRA,
    MediaPlayerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_IDLE, STATE_OFF, STATE_PAUSED, STATE_PLAYING
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from music_assistant.common.helpers.datetime import from_utc_timestamp
from music_assistant.common.models.enums import (
    EventType,
    MediaType,
    PlayerState,
    QueueOption,
    RepeatMode,
)
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.media_items import MediaItemType

from .const import (
    ATTR_ACTIVE_QUEUE,
    ATTR_GROUP_LEADER,
    ATTR_GROUP_MEMBERS,
    ATTR_MASS_PLAYER_ID,
    ATTR_MASS_PLAYER_TYPE,
    ATTR_QUEUE_INDEX,
    ATTR_QUEUE_ITEMS,
    DOMAIN,
)
from .entity import MassBaseEntity
from .helpers import get_mass
from .media_browser import async_browse_media

if TYPE_CHECKING:
    from music_assistant.client import MusicAssistantClient
    from music_assistant.common.models.player_queue import PlayerQueue

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.CLEAR_PLAYLIST
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.MEDIA_ENQUEUE
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
)

STATE_MAPPING = {
    PlayerState.IDLE: STATE_IDLE,
    PlayerState.PLAYING: STATE_PLAYING,
    PlayerState.PAUSED: STATE_PAUSED,
}

QUEUE_OPTION_MAP = {
    # map from HA enqueue options to MA enqueue options
    # which are the same but just in case
    MediaPlayerEnqueue.ADD: QueueOption.ADD,
    MediaPlayerEnqueue.NEXT: QueueOption.NEXT,
    MediaPlayerEnqueue.PLAY: QueueOption.PLAY,
    MediaPlayerEnqueue.REPLACE: QueueOption.REPLACE,
}

SERVICE_PLAY_MEDIA_ADVANCED = "play_media"
ATTR_RADIO_MODE = "radio_mode"
ATTR_MEDIA_ID = "media_id"
ATTR_MEDIA_TYPE = "media_type"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Music Assistant MediaPlayer(s) from Config Entry."""
    mass = get_mass(hass, config_entry.entry_id)
    added_ids = set()

    async def handle_player_added(event: MassEvent) -> None:
        """Handle Mass Player Added event."""
        if event.object_id in added_ids:
            return
        added_ids.add(event.object_id)
        async_add_entities([MassPlayer(mass, event.object_id)])

    # register listener for new players
    config_entry.async_on_unload(mass.subscribe(handle_player_added, EventType.PLAYER_ADDED))
    # add all current players
    for player in mass.players:
        added_ids.add(player.player_id)
        async_add_entities([MassPlayer(mass, player.player_id)])

    # add platform service for play_media with advanced options
    platform = async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_PLAY_MEDIA_ADVANCED,
        {
            vol.Optional(ATTR_MEDIA_TYPE): vol.Coerce(MediaType),
            vol.Required(ATTR_MEDIA_ID): vol.All(cv.ensure_list, [cv.string]),
            vol.Exclusive(ATTR_MEDIA_ENQUEUE, "enqueue_announce"): vol.Coerce(QueueOption),
            vol.Exclusive(ATTR_MEDIA_ANNOUNCE, "enqueue_announce"): cv.boolean,
            vol.Optional(ATTR_RADIO_MODE): vol.Coerce(bool),
        },
        "_async_play_media_advanced",
    )


class MassPlayer(MassBaseEntity, MediaPlayerEntity):
    """Representation of MediaPlayerEntity from Music Assistant Player."""

    _attr_name = None

    def __init__(self, mass: MusicAssistantClient, player_id: str) -> None:
        """Initialize MediaPlayer entity."""
        super().__init__(mass, player_id)
        self._attr_media_image_remotely_accessible = True
        self._attr_supported_features = SUPPORTED_FEATURES
        self._attr_device_class = MediaPlayerDeviceClass.SPEAKER
        self._attr_media_position_updated_at = None
        self._attr_media_position = None
        self._attr_media_duration = None
        self._attr_media_album_artist = None
        self._attr_media_artist = None
        self._attr_media_album_name = None
        self._attr_media_title = None
        self._attr_media_content_id = None
        self._attr_media_content_type = "music"
        self._attr_media_image_url = None
        self._prev_time = 0

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        await super().async_added_to_hass()

        # we subscribe to player queue time update but we only
        # accept a state change on big time jumps (e.g. seeking)
        async def queue_time_updated(event: MassEvent):
            if event.object_id != self.player.active_source:
                return
            if abs(self._prev_time - event.data) > 5:
                await self.async_on_update()
                self.async_write_ha_state()
            self._prev_time = event.data

        self.async_on_remove(
            self.mass.subscribe(
                queue_time_updated,
                EventType.QUEUE_TIME_UPDATED,
            )
        )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return additional state attributes."""
        player = self.player
        queue = self.mass.players.get_player_queue(player.active_source)
        return {
            ATTR_MASS_PLAYER_ID: self.player_id,
            ATTR_MASS_PLAYER_TYPE: player.type.value,
            ATTR_GROUP_MEMBERS: player.group_childs,
            ATTR_GROUP_LEADER: player.synced_to,
            ATTR_ACTIVE_QUEUE: player.active_source,
            ATTR_QUEUE_ITEMS: queue.items if queue else None,
            ATTR_QUEUE_INDEX: queue.current_index if queue else None,
        }

    async def async_on_update(self) -> None:
        """Handle player updates."""
        if not self.available:
            return
        player = self.player
        queue = self.mass.players.get_player_queue(player.active_source)
        # update generic attributes
        if player.powered:
            self._attr_state = STATE_MAPPING[self.player.state]
        else:
            self._attr_state = STATE_OFF
        self._attr_app_id = DOMAIN if queue else player.active_source
        self._attr_shuffle = queue.shuffle_enabled if queue else None
        self._attr_repeat = queue.repeat_mode.value if queue else None
        self._attr_group_members = player.group_childs
        self._attr_volume_level = player.volume_level / 100
        self._attr_is_volume_muted = player.volume_muted
        if queue is not None:
            self._attr_media_position = queue.elapsed_time
            self._attr_media_position_updated_at = from_utc_timestamp(
                queue.elapsed_time_last_updated
            )
        else:
            self._attr_media_position = player.elapsed_time
            self._attr_media_position_updated_at = from_utc_timestamp(
                player.elapsed_time_last_updated
            )
        self._prev_time = queue.elapsed_time
        self._update_media_image_url(queue)
        # update current media item infos
        media_artist = None
        media_album_artist = None
        media_album_name = None
        media_title = player.active_source
        media_content_id = player.current_url
        media_duration = None
        # Music Assistant is the active source and actually has a MediaItem loaded
        if queue and queue.current_item and queue.current_item.media_item:
            media_item = queue.current_item.media_item
            media_title = media_item.name
            media_content_id = queue.current_item.uri
            media_duration = queue.current_item.duration
            if media_item.media_type == MediaType.TRACK:
                media_artist = ", ".join([x.name for x in media_item.artists])
                if media_item.version:
                    media_title += f" ({media_item.version})"
                if media_item.album:
                    media_album_name = media_item.album.name
                    if getattr(media_item.album, "artist", None):
                        media_album_artist = media_item.album.artist.name
        # set the attributes
        self._attr_media_artist = media_artist
        self._attr_media_album_artist = media_album_artist
        self._attr_media_album_name = media_album_name
        self._attr_media_title = media_title
        self._attr_media_content_id = media_content_id
        self._attr_media_duration = media_duration

    def _update_media_image_url(self, queue: PlayerQueue) -> None:
        """Update image URL for the active queue item."""
        if queue is None or queue.current_item is None:
            self._attr_media_image_url = None
            return
        if image := queue.current_item.image:
            self._attr_media_image_remotely_accessible = image.provider == "url"
            self._attr_media_image_url = self.mass.get_image_url(image)
            return
        self._attr_media_image_url = None

    async def async_media_play(self) -> None:
        """Send play command to device."""
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_play(queue.queue_id)
        else:
            await self.mass.players.queue_command_play(self.player_id)

    async def async_media_pause(self) -> None:
        """Send pause command to device."""
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_pause(queue.queue_id)
        else:
            await self.mass.players.queue_command_pause(self.player_id)

    async def async_media_stop(self) -> None:
        """Send stop command to device."""
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_stop(queue.queue_id)
        else:
            await self.mass.players.queue_command_stop(self.player_id)

    async def async_media_next_track(self) -> None:
        """Send next track command to device."""
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_next(queue.queue_id)
        else:
            await self.mass.players.queue_command_next(self.player_id)

    async def async_media_previous_track(self) -> None:
        """Send previous track command to device."""
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_previous(queue.queue_id)
        else:
            await self.mass.players.queue_command_previous(self.player_id)

    async def async_media_seek(self, position: float) -> None:
        """Send seek command."""
        position = int(position)
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_seek(queue.queue_id, position)
        else:
            await self.mass.players.queue_command_seek(self.player_id, position)

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute the volume."""
        await self.mass.players.player_command_volume_mute(self.player_id, mute)

    async def async_set_volume_level(self, volume: float) -> None:
        """Send new volume_level to device."""
        volume = int(volume * 100)
        await self.mass.players.player_command_volume_set(self.player_id, volume)

    async def async_volume_up(self) -> None:
        """Send new volume_level to device."""
        await self.mass.players.player_command_volume_up(self.player_id)

    async def async_volume_down(self) -> None:
        """Send new volume_level to device."""
        await self.mass.players.player_command_volume_down(self.player_id)

    async def async_turn_on(self) -> None:
        """Turn on device."""
        await self.mass.players.player_command_power(self.player_id, True)

    async def async_turn_off(self) -> None:
        """Turn off device."""
        await self.mass.players.player_command_power(self.player_id, False)

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Set shuffle state."""
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_shuffle(queue.queue_id, shuffle)
        else:
            await self.mass.players.queue_command_shuffle(self.player_id, shuffle)

    async def async_set_repeat(self, repeat: str) -> None:
        """Set repeat state."""
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_repeat(queue.queue_id, RepeatMode(repeat))
        else:
            await self.mass.players.queue_command_repeat(self.player_id, RepeatMode(repeat))

    async def async_clear_playlist(self) -> None:
        """Clear players playlist."""
        if queue := self.mass.players.get_player_queue(self.player.active_source):
            await self.mass.players.queue_command_clear(queue.queue_id)
        else:
            await self.mass.players.queue_command_clear(self.player_id)

    async def async_play_media(
        self,
        media_type: str,
        media_id: str,
        enqueue: MediaPlayerEnqueue | None = None,
        announce: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """Send the play_media command to the media player."""
        if media_source.is_media_source_id(media_id):
            # Handle media_source
            sourced_media = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = sourced_media.url
            media_id = async_process_play_media_url(self.hass, media_id)

        # forward to our advanced play_media handler
        await self._async_play_media_advanced(
            media_id=[media_id],
            enqueue=enqueue,
            announce=announce,
            media_type=media_type,
            radio_mode=kwargs[ATTR_MEDIA_EXTRA].get(ATTR_RADIO_MODE),
        )

    async def _async_play_media_advanced(
        self,
        media_id: list[str],
        enqueue: MediaPlayerEnqueue | QueueOption | None = QueueOption.PLAY,
        announce: bool | None = None,  # noqa: ARG002
        radio_mode: bool | None = None,  # noqa: ARG002
        media_type: str | None = None,  # noqa: ARG002
    ) -> None:
        """Send the play_media command to the media player."""
        # pylint: disable=too-many-arguments
        media_uris: list[str] = []
        # work out (all) uri(s) to play
        for media_id_str in media_id:
            # prefer URI format
            if "://" in media_id_str:
                media_uris.append(media_id_str)
                continue
            # try content id as library id
            if media_type and media_id_str.isnumeric():
                with suppress(MediaNotFoundError):
                    item = await self.mass.music.get_item(media_type, media_id_str, "library")
                    media_uris.append(item.uri)
                    continue
            # lookup by name
            if item := await self._get_item_by_name(media_id_str, media_type):
                media_uris.append(item.uri)

        if not media_uris:
            return

        if queue := self.mass.players.get_player_queue(self.player.active_source):
            queue_id = queue.queue_id
        else:
            queue_id = self.player_id
        await self.mass.players.play_media(
            queue_id, media=media_uris, option=enqueue, radio_mode=radio_mode
        )

        # announce/alert support
        # is_tts = "/api/tts_proxy" in media_id
        # if announce or is_tts:
        #     self.hass.create_task(
        #         self.player.active_queue.play_announcement(media_id, is_tts)
        #     )
        # else:
        #     await self.player.active_queue.play_media(media_id, queue_opt)

    async def async_browse_media(
        self, media_content_type=None, media_content_id=None
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper."""
        return await async_browse_media(self.hass, self.mass, media_content_id, media_content_type)

    async def _get_item_by_name(
        self, name: str, media_type: str | None = None
    ) -> MediaItemType | None:
        """Try to find a media item (such as a playlist) by name."""
        # pylint: disable=too-many-nested-blocks
        searchname = name.lower()
        library_functions = [
            x
            for x in (
                self.mass.music.get_library_playlists,
                self.mass.music.get_library_radios,
                self.mass.music.get_library_albums,
                self.mass.music.get_library_tracks,
                self.mass.music.get_library_artists,
            )
            if not media_type or media_type.lower() in x.__name__
        ]
        if not media_type:
            # address (possible) voice command with mediatype in search string
            for media_type_str in ("artist", "album", "track", "playlist"):
                media_type_subst_str = f"{media_type_str} "
                if media_type_subst_str in searchname:
                    media_type = MediaType(media_type_str)
                    searchname = searchname.replace(media_type_subst_str, "")
                    break

        # prefer (exact) lookup in the library by name
        for func in library_functions:
            result = await func(search=searchname)
            for item in result.items:
                if searchname == item.name.lower():
                    return item
            # repeat but account for tracks or albums where an artist name is used
            if func in (self.mass.music.get_library_tracks, self.mass.music.get_library_albums):
                for splitter in (" - ", " by "):
                    if splitter in searchname:
                        artistname, title = searchname.split(splitter, 1)
                        result = await func(search=title)
                        for item in result.items:
                            if item.name.lower() != title:
                                continue
                            for artist in item.artists:
                                if artist.name.lower() == artistname:
                                    return item
        # nothing found in the library, fallback to search
        result = await self.mass.music.search(
            searchname, media_types=[media_type] if media_type else MediaType.ALL
        )
        for results in (
            result.tracks,
            result.albums,
            result.playlists,
            result.artists,
            result.radio,
        ):
            for item in results:
                # simply return the first item because search is already sorted by best match
                return item
        return None

"""Publishing tests for the Instagram-login ("Instagram (Direct)") provider."""

from unittest.mock import MagicMock, patch

import pytest

from providers import instagram, instagram_login
from providers.base import is_video_url
from providers.exceptions import PublishError
from providers.instagram_login import InstagramLoginProvider
from providers.types import PostType, PublishContent


def _provider():
    return InstagramLoginProvider({"client_id": "id", "client_secret": "secret"})


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


def _publish_single(content):
    """Run publish_post through the create → poll → publish flow; return (result, create payload)."""
    provider = _provider()
    provider._request = MagicMock(
        side_effect=[
            _resp({"id": "container-1"}),
            _resp({"status_code": "FINISHED"}),
            _resp({"id": "media-1"}),
        ]
    )
    result = provider.publish_post("tok", content)
    create_call = provider._request.call_args_list[0]
    assert create_call.args[1].endswith("/me/media")
    return result, create_call.kwargs["json"]


def test_lone_video_publishes_as_reel():
    # The engine types a single video asset as VIDEO. Instagram has no
    # standalone feed videos, so it must take the REELS path — previously the
    # .mp4 fell through to the image branch and was sent as image_url.
    result, payload = _publish_single(
        PublishContent(text="hi", media_urls=["https://cdn.example/clip.mp4"], post_type=PostType.VIDEO)
    )
    assert payload == {"caption": "hi", "media_type": "REELS", "video_url": "https://cdn.example/clip.mp4"}
    assert result.platform_post_id == "media-1"


def test_reel_hint_publishes_as_reel():
    _, payload = _publish_single(PublishContent(media_urls=["https://cdn.example/clip.mp4"], post_type=PostType.REEL))
    assert payload == {"media_type": "REELS", "video_url": "https://cdn.example/clip.mp4"}


@pytest.mark.parametrize(
    ("url", "field"),
    [
        ("https://cdn.example/frame.jpg", "image_url"),
        ("https://cdn.example/clip.MOV?X-Amz-Signature=abc", "video_url"),
    ],
)
def test_story_routes_image_and_video(url, field):
    _, payload = _publish_single(PublishContent(media_urls=[url], post_type=PostType.STORY))
    assert payload == {"media_type": "STORIES", field: url}


def test_single_image_stays_a_feed_image():
    _, payload = _publish_single(PublishContent(media_urls=["https://cdn.example/a.png"], post_type=PostType.IMAGE))
    assert payload == {"image_url": "https://cdn.example/a.png"}


def test_carousel_child_video_detected_despite_query_string():
    provider = _provider()
    provider._request = MagicMock(
        side_effect=[
            _resp({"id": "child-1"}),
            _resp({"status_code": "FINISHED"}),
            _resp({"id": "child-2"}),
            _resp({"status_code": "FINISHED"}),
            _resp({"id": "carousel"}),
            _resp({"status_code": "FINISHED"}),
            _resp({"id": "media-9"}),
        ]
    )
    provider.publish_post(
        "tok",
        PublishContent(
            media_urls=[
                "https://r2.example/clip.mp4?X-Amz-Signature=abc",
                "https://r2.example/a.jpg?X-Amz-Signature=def",
            ],
            post_type=PostType.CAROUSEL,
        ),
    )
    first_child = provider._request.call_args_list[0].kwargs["json"]
    second_child = provider._request.call_args_list[2].kwargs["json"]
    assert first_child == {
        "is_carousel_item": True,
        "media_type": "VIDEO",
        "video_url": "https://r2.example/clip.mp4?X-Amz-Signature=abc",
    }
    assert second_child == {"is_carousel_item": True, "image_url": "https://r2.example/a.jpg?X-Amz-Signature=def"}


@pytest.mark.parametrize("module", [instagram, instagram_login])
def test_container_polling_follows_meta_guidance(module):
    # Meta: query a container's status about once per minute, for at most five
    # minutes. Two-second polling across a 14-item carousel (repeated on every
    # retry) hit the app-level request limit.
    assert module.CONTAINER_POLL_INTERVAL >= 10
    assert module.CONTAINER_POLL_INTERVAL * module.CONTAINER_POLL_MAX_ATTEMPTS <= 300


def test_wait_for_container_sleeps_between_polls_then_times_out():
    provider = _provider()
    provider._request = MagicMock(return_value=_resp({"status_code": "IN_PROGRESS"}))
    with patch("providers.instagram_login.time.sleep") as sleep, pytest.raises(PublishError, match="timed out"):
        provider._wait_for_container("tok", "c1")
    assert provider._request.call_count == instagram_login.CONTAINER_POLL_MAX_ATTEMPTS
    sleep.assert_called_with(instagram_login.CONTAINER_POLL_INTERVAL)


def test_wait_for_container_surfaces_error_status():
    provider = _provider()
    provider._request = MagicMock(return_value=_resp({"status_code": "ERROR", "status": "Error: unsupported"}))
    with patch("providers.instagram_login.time.sleep") as sleep, pytest.raises(PublishError, match="unsupported"):
        provider._wait_for_container("tok", "c1")
    sleep.assert_not_called()


def test_is_video_url_inspects_only_the_path():
    assert is_video_url("https://r2.example/clip.mp4?X-Amz-Signature=abc")
    assert is_video_url("https://cdn.example/CLIP.MOV")
    assert not is_video_url("https://cdn.example/photo.jpg")
    assert not is_video_url("https://cdn.example/photo.jpg?next=clip.mp4")

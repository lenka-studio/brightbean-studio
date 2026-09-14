"""Instagram post type (feed / reel / story) selection in the composer.

The publish engine only produces a Reel or Story when ``platform_extra`` carries
a ``post_type`` hint. The composer panel writes that hint, and publish-bound
saves are rejected when the attached media can't become the chosen type (or
would exceed Instagram's carousel limit) so the failure shows up in the
composer rather than in the worker hours later.
"""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class InstagramPostTypeTestsBase(TestCase):
    platform = "instagram_login"

    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.client.force_login(self.user)

        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform=self.platform,
            account_platform_id="ig-1",
            account_name="Lenka Studio",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.post = Post.objects.create(workspace=self.workspace, author=self.user, caption="hello")
        self.save_url = reverse(
            "composer:save_post_edit",
            kwargs={"workspace_id": self.workspace.id, "post_id": self.post.id},
        )
        self.field = f"ig_post_type_{self.account.id}"

    def _asset(self, media_type, name=None):
        return MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            filename=name or f"asset.{'mp4' if media_type == 'video' else 'png'}",
            media_type=media_type,
            file_size=1,
            processing_status=MediaAsset.ProcessingStatus.COMPLETED,
        )

    def _attach(self, *media_types):
        for idx, media_type in enumerate(media_types):
            PostMedia.objects.create(post=self.post, media_asset=self._asset(media_type), position=idx)

    def _payload(self, action="save_draft", **overrides):
        payload = {
            "action": action,
            "title": "",
            "caption": "hello",
            "tags": "",
            "selected_accounts": str(self.account.id),
        }
        if action == "schedule":
            tomorrow = timezone.now() + timedelta(days=1)
            payload["scheduled_date"] = tomorrow.date().isoformat()
            payload["scheduled_time"] = "10:00"
        payload.update(overrides)
        return payload

    def _platform_post(self):
        return PlatformPost.objects.get(post=self.post, social_account=self.account)

    def _error(self, response):
        self.assertEqual(response.status_code, 400)
        return response.json()["errors"]["instagram_media"]


class PostTypeHintTests(InstagramPostTypeTestsBase):
    def test_reel_selection_is_saved_as_post_type_hint(self):
        PlatformPost.objects.create(post=self.post, social_account=self.account, platform_extra={"id": "resp-1"})
        response = self.client.post(self.save_url, data=self._payload(**{self.field: "reel"}))
        self.assertIn(response.status_code, (200, 204, 302))
        # Merged, not replaced: keys the engine stored from the publish response survive.
        self.assertEqual(self._platform_post().platform_extra, {"id": "resp-1", "post_type": "reel"})

    def test_story_selection_is_saved(self):
        self.client.post(self.save_url, data=self._payload(**{self.field: "story"}))
        self.assertEqual(self._platform_post().platform_extra.get("post_type"), "story")

    def test_feed_selection_clears_hint(self):
        PlatformPost.objects.create(post=self.post, social_account=self.account, platform_extra={"post_type": "reel"})
        self.client.post(self.save_url, data=self._payload(**{self.field: ""}))
        self.assertNotIn("post_type", self._platform_post().platform_extra)

    def test_unknown_value_is_ignored(self):
        self.client.post(self.save_url, data=self._payload(**{self.field: "live"}))
        self.assertNotIn("post_type", self._platform_post().platform_extra)

    def test_missing_panel_leaves_hint_untouched(self):
        # A save that didn't render the Instagram panel (e.g. another form)
        # must not wipe a previously chosen type.
        PlatformPost.objects.create(post=self.post, social_account=self.account, platform_extra={"post_type": "story"})
        self.client.post(self.save_url, data=self._payload())
        self.assertEqual(self._platform_post().platform_extra.get("post_type"), "story")


class PostTypeHintFacebookLoginProviderTests(PostTypeHintTests):
    """The Facebook-login Instagram provider shares the same panel and hint."""

    platform = "instagram"


class PublishBoundValidationTests(InstagramPostTypeTestsBase):
    def test_reel_with_two_images_is_rejected_at_schedule(self):
        self._attach("image", "image")
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: "reel"}))
        self.assertIn("Reels need exactly one video", self._error(response))
        self.assertFalse(PlatformPost.objects.filter(post=self.post, status="scheduled").exists())

    def test_reel_with_one_image_is_rejected(self):
        self._attach("image")
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: "reel"}))
        self.assertIn("Reels need exactly one video", self._error(response))

    def test_reel_with_one_video_schedules(self):
        self._attach("video")
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: "reel"}))
        self.assertIn(response.status_code, (200, 204, 302))
        pp = self._platform_post()
        self.assertEqual(pp.status, PlatformPost.Status.SCHEDULED)
        self.assertEqual(pp.platform_extra.get("post_type"), "reel")

    def test_story_without_media_is_rejected(self):
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: "story"}))
        self.assertIn("Stories need exactly one image or video", self._error(response))

    def test_story_with_one_image_schedules(self):
        self._attach("image")
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: "story"}))
        self.assertIn(response.status_code, (200, 204, 302))
        self.assertEqual(self._platform_post().status, PlatformPost.Status.SCHEDULED)

    def test_story_with_two_items_is_rejected(self):
        self._attach("image", "video")
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: "story"}))
        self.assertIn("Stories need exactly one image or video", self._error(response))

    def test_carousel_over_ten_items_is_rejected(self):
        self._attach(*(["image"] * 11))
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: ""}))
        self.assertIn("at most 10 items", self._error(response))

    def test_carousel_of_ten_items_schedules(self):
        self._attach(*(["video"] + ["image"] * 9))
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: ""}))
        self.assertIn(response.status_code, (200, 204, 302))
        self.assertEqual(self._platform_post().status, PlatformPost.Status.SCHEDULED)

    def test_feed_post_without_media_is_rejected(self):
        response = self.client.post(self.save_url, data=self._payload("schedule", **{self.field: ""}))
        self.assertIn("at least one image or video", self._error(response))

    def test_saved_hint_is_used_when_panel_absent(self):
        # The hint chosen earlier still governs validation when the form that
        # submits the schedule didn't include the Instagram panel.
        PlatformPost.objects.create(post=self.post, social_account=self.account, platform_extra={"post_type": "reel"})
        self._attach("image")
        response = self.client.post(self.save_url, data=self._payload("schedule"))
        self.assertIn("Reels need exactly one video", self._error(response))

    def test_submit_for_approval_is_validated(self):
        self._attach("image", "image")
        response = self.client.post(self.save_url, data=self._payload("submit_for_approval", **{self.field: "reel"}))
        self.assertIn("Reels need exactly one video", self._error(response))

    def test_draft_save_is_not_validated(self):
        response = self.client.post(self.save_url, data=self._payload("save_draft", **{self.field: "reel"}))
        self.assertIn(response.status_code, (200, 204, 302))
        self.assertEqual(self._platform_post().platform_extra.get("post_type"), "reel")

    def test_non_instagram_accounts_are_not_affected(self):
        tiktok = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="tiktok",
            account_platform_id="tt-1",
            account_name="tt",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        response = self.client.post(self.save_url, data=self._payload("schedule", selected_accounts=str(tiktok.id)))
        self.assertIn(response.status_code, (200, 204, 302))


class NewPostPendingMediaTests(InstagramPostTypeTestsBase):
    """A brand-new post's media lives in the pending-media session list until save."""

    def setUp(self):
        super().setUp()
        self.create_url = reverse("composer:save_post", kwargs={"workspace_id": self.workspace.id})

    def _set_pending(self, *assets):
        session = self.client.session
        session[f"pending_media_{self.workspace.id}"] = [str(a.id) for a in assets]
        session.save()

    def test_pending_video_satisfies_reel(self):
        self._set_pending(self._asset("video"))
        response = self.client.post(self.create_url, data=self._payload("schedule", **{self.field: "reel"}))
        self.assertIn(response.status_code, (200, 204, 302))
        pp = PlatformPost.objects.get(social_account=self.account, post__caption="hello", status="scheduled")
        self.assertEqual(pp.platform_extra.get("post_type"), "reel")
        self.assertEqual(pp.post.media_attachments.count(), 1)

    def test_pending_images_cannot_become_a_reel(self):
        self._set_pending(self._asset("image"), self._asset("image"))
        response = self.client.post(self.create_url, data=self._payload("schedule", **{self.field: "reel"}))
        self.assertIn("Reels need exactly one video", self._error(response))
        self.assertFalse(
            Post.objects.filter(caption="hello", platform_posts__isnull=False).exclude(id=self.post.id).exists()
        )


class ComposerPanelRenderTests(InstagramPostTypeTestsBase):
    def test_compose_page_renders_post_type_selector(self):
        PlatformPost.objects.create(post=self.post, social_account=self.account, platform_extra={"post_type": "reel"})
        url = reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": self.post.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("'ig_post_type_' + accId", html)
        self.assertIn("Reel (one video)", html)
        self.assertIn("Story (one image or video", html)
        # The saved hint reaches the page so the select pre-selects it.
        self.assertIn('"post_type": "reel"', html)

"""The API adapters of the plugin marketing: one small class for each channel with a posting API.

Each builds one request from a ``Content`` and the channel's credential, sends it through the transport it was given
and reads the platform's id of the post out of the answer. The endpoints and fields are [Unverified] against each
platform's current API documentation (``docs/marketing-plugin.md`` names the page of each); a platform that changed
answers an error, which the breaker counts, and nothing else breaks. No adapter reads a feed or a metric of Facebook or
TikTok: those two post only (plan autonomous-oss-growth, non-goals).

``health`` is the read-only check of an account (plan autonomous-oss-growth, step 22): one read of the platform that
says the credential works and may post as the account, and posts nothing. It answers what it read, or raises
AdapterError with the reason; neither holds the credential.
"""

from __future__ import annotations

from evo_agents.core_plugins.marketing.adapters.base import (
    AdapterError,
    Content,
    HttpRequest,
    Published,
    Transport,
    check,
)


def _bearer(token: str, **more: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", **more}


class _Base:
    channel = ""

    def __init__(self, transport: Transport):
        self.transport = transport

    async def call(self, request: HttpRequest) -> dict:
        return check(await self.transport.send(request), f"{self.channel} post")

    async def read(self, request: HttpRequest) -> dict:
        """The body of a read of the health check; AdapterError as ``call`` otherwise."""
        return check(await self.transport.send(request), f"{self.channel} check")


class GitHub(_Base):
    """A Release of a repo (target ``owner/name``) or a Discussion (target ``owner/name#<repositoryId>:<categoryId>``)."""

    channel = "github"
    API = "https://api.github.com"

    async def publish(self, content: Content, token: str) -> Published:
        target = content.target or ""
        repo, _, discussion = target.partition("#")
        if repo.count("/") != 1:
            raise AdapterError("missing", "github: the account has no target owner/name")
        headers = _bearer(token, **{"X-GitHub-Api-Version": "2022-11-28"})
        if content.reply_to:
            return await self._reply(repo, content, headers)
        if discussion:
            return await self._discussion(content, discussion, headers)
        tag = content.title or "release"
        body = await self.call(
            HttpRequest(
                "POST",
                f"{self.API}/repos/{repo}/releases",
                headers,
                {"tag_name": tag, "name": tag, "body": content.body},
            )
        )
        return Published(str(body.get("html_url") or f"https://github.com/{repo}/releases"), str(body.get("id", "")))

    async def health(self, target: str | None, token: str) -> str:
        """The target repo read with the credential, which must have push on it (Releases and Discussions need it)."""
        repo = (target or "").partition("#")[0]
        if repo.count("/") != 1:
            raise AdapterError("missing", "github: the account has no target owner/name")
        headers = _bearer(token, **{"X-GitHub-Api-Version": "2022-11-28"})
        body = await self.read(HttpRequest("GET", f"{self.API}/repos/{repo}", headers))
        if not (body.get("permissions") or {}).get("push"):
            raise AdapterError("rejected", f"github: the credential reads {repo} but may not push to it")
        return f"push on {body.get('full_name') or repo}"

    async def _discussion(self, content: Content, ids: str, headers: dict) -> Published:
        repository, _, category = ids.partition(":")
        query = (
            "mutation($r:ID!,$c:ID!,$t:String!,$b:String!){createDiscussion(input:{repositoryId:$r,categoryId:$c,"
            "title:$t,body:$b}){discussion{id url}}}"
        )
        variables = {"r": repository, "c": category, "t": content.title or content.body[:80], "b": content.body}
        body = await self.call(
            HttpRequest("POST", f"{self.API}/graphql", headers, {"query": query, "variables": variables})
        )
        found = ((body.get("data") or {}).get("createDiscussion") or {}).get("discussion") or {}
        if not found:
            raise AdapterError("rejected", "github: the discussion was not created")
        return Published(str(found.get("url", "")), str(found.get("id", "")))

    async def _reply(self, repo: str, content: Content, headers: dict) -> Published:
        """A comment under an issue or a pull request (``issue#<n>``, ``pr#<n>``), or under a discussion
        (``discussion#<n>``, or the node id of a discussion or of a comment to answer in its thread)."""
        kind, _, number = (content.reply_to or "").partition("#")
        if kind in ("issue", "pr") and number.isdigit():
            body = await self.call(
                HttpRequest(
                    "POST", f"{self.API}/repos/{repo}/issues/{number}/comments", headers, {"body": content.body}
                )
            )
            return Published(str(body.get("html_url", "")), str(body.get("id", "")))
        if kind == "discussion" and number.isdigit():
            owner, _, name = repo.partition("/")
            query = "query($o:String!,$n:String!,$d:Int!){repository(owner:$o,name:$n){discussion(number:$d){id}}}"
            variables = {"o": owner, "n": name, "d": int(number)}
            found = await self.call(
                HttpRequest("POST", f"{self.API}/graphql", headers, {"query": query, "variables": variables})
            )
            node = (((found.get("data") or {}).get("repository") or {}).get("discussion") or {}).get("id")
            if not node:
                raise AdapterError("rejected", "github: no such discussion to answer")
            return await self._comment(str(node), None, content.body, headers)
        discussion, _, comment = (content.reply_to or "").partition(":")
        return await self._comment(discussion, comment or None, content.body, headers)

    async def _comment(self, discussion: str, comment: str | None, text: str, headers: dict) -> Published:
        query = (
            "mutation($d:ID!,$b:String!,$r:ID){addDiscussionComment(input:{discussionId:$d,body:$b,replyToId:$r})"
            "{comment{id url}}}"
        )
        variables = {"d": discussion, "b": text, "r": comment}
        body = await self.call(
            HttpRequest("POST", f"{self.API}/graphql", headers, {"query": query, "variables": variables})
        )
        found = ((body.get("data") or {}).get("addDiscussionComment") or {}).get("comment") or {}
        if not found:
            raise AdapterError("rejected", "github: the comment was not made")
        return Published(str(found.get("url", "")), str(found.get("id", "")))

    async def replies(self, external_id: str, token: str) -> list[dict]:
        """The comments under a discussion the account started (its node id); a release has none to read."""
        if not external_id.startswith("D_"):
            return []
        query = "query($d:ID!){node(id:$d){... on Discussion{comments(last:50){nodes{id url body author{login}}}}}}"
        headers = _bearer(token, **{"X-GitHub-Api-Version": "2022-11-28"})
        body = await self.call(
            HttpRequest("POST", f"{self.API}/graphql", headers, {"query": query, "variables": {"d": external_id}})
        )
        nodes = (((body.get("data") or {}).get("node") or {}).get("comments") or {}).get("nodes") or []
        return [
            {
                "external_id": f"{external_id}:{node.get('id')}",
                "author": (node.get("author") or {}).get("login"),
                "url": node.get("url"),
                "body": str(node.get("body") or ""),
            }
            for node in nodes
            if isinstance(node, dict) and node.get("id")
        ]


class X(_Base):
    channel = "x"
    API = "https://api.x.com/2/tweets"

    async def publish(self, content: Content, token: str) -> Published:
        payload: dict = {"text": content.body}
        if content.reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": content.reply_to}
        body = await self.call(HttpRequest("POST", self.API, _bearer(token), payload))
        found = str((body.get("data") or {}).get("id", ""))
        return Published(f"https://x.com/{content.account}/status/{found}", found, cost_usd=0.01)

    async def health(self, target: str | None, token: str) -> str:
        """The user the credential acts as."""
        body = await self.read(HttpRequest("GET", "https://api.x.com/2/users/me", _bearer(token)))
        user = body.get("data") or {}
        if not user.get("id"):
            raise AdapterError("rejected", "x: the platform did not say whose credential it is")
        return f"signed in as @{user.get('username') or user['id']}"

    async def metrics(self, external_id: str, token: str) -> dict[str, float]:
        url = f"{self.API}/{external_id}?tweet.fields=public_metrics"
        found = ((await self.call(HttpRequest("GET", url, _bearer(token)))).get("data") or {}).get(
            "public_metrics"
        ) or {}
        names = {
            "like_count": "likes",
            "retweet_count": "reposts",
            "reply_count": "replies",
            "impression_count": "impressions",
        }
        return {name: float(found[key]) for key, name in names.items() if isinstance(found.get(key), (int, float))}


class LinkedIn(_Base):
    """A post as the author URN of the account (target ``urn:li:person:...`` or ``urn:li:organization:...``)."""

    channel = "linkedin"
    API = "https://api.linkedin.com/rest/posts"

    async def publish(self, content: Content, token: str) -> Published:
        if not (content.target or "").startswith("urn:li:"):
            raise AdapterError("missing", "linkedin: the account has no target author urn")
        payload = {
            "author": content.target,
            "commentary": content.body,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED"},
            "lifecycleState": "PUBLISHED",
        }
        headers = _bearer(token, **{"Linkedin-Version": "202401", "X-Restli-Protocol-Version": "2.0.0"})
        answer = await self.transport.send(HttpRequest("POST", self.API, headers, payload))
        check(answer, "linkedin post")
        urn = answer.headers.get("x-restli-id") or str(answer.body.get("id", ""))
        return Published(f"https://www.linkedin.com/feed/update/{urn}", urn)

    async def health(self, target: str | None, token: str) -> str:
        """The member the credential acts as (OpenID userinfo), with an author urn to post as."""
        if not (target or "").startswith("urn:li:"):
            raise AdapterError("missing", "linkedin: the account has no target author urn")
        body = await self.read(HttpRequest("GET", "https://api.linkedin.com/v2/userinfo", _bearer(token)))
        if not body.get("sub"):
            raise AdapterError("rejected", "linkedin: the platform did not say whose credential it is")
        return f"signed in as {body.get('name') or body['sub']}, posting as {target}"


class Reddit(_Base):
    """A self post in the subreddit of the target (``r/name``), or a comment under ``reply_to`` (a thing id)."""

    channel = "reddit"
    API = "https://oauth.reddit.com/api"

    async def publish(self, content: Content, token: str) -> Published:
        sub = (content.target or "").removeprefix("r/")
        if not sub and not content.reply_to:
            raise AdapterError("missing", "reddit: the account has no target subreddit")
        if content.reply_to:
            form = {"api_type": "json", "thing_id": content.reply_to, "text": content.body}
            path = "comment"
        else:
            form = {
                "api_type": "json",
                "kind": "self",
                "sr": sub,
                "title": content.title or content.body[:100],
                "text": content.body,
            }
            path = "submit"
        headers = _bearer(token, **{"User-Agent": "evo-agents-marketing/1"})
        body = await self.call(HttpRequest("POST", f"{self.API}/{path}", headers, form=form))
        data = (body.get("json") or {}).get("data") or {}
        if (body.get("json") or {}).get("errors"):
            raise AdapterError("rejected", "reddit: the post was refused")
        return Published(str(data.get("url", "")), str(data.get("name") or data.get("id") or ""))

    async def health(self, target: str | None, token: str) -> str:
        """The user the credential acts as."""
        headers = _bearer(token, **{"User-Agent": "evo-agents-marketing/1"})
        body = await self.read(HttpRequest("GET", "https://oauth.reddit.com/api/v1/me", headers))
        if not body.get("name"):
            raise AdapterError("rejected", "reddit: the platform did not say whose credential it is")
        return f"signed in as u/{body['name']}"

    async def metrics(self, external_id: str, token: str) -> dict[str, float]:
        headers = _bearer(token, **{"User-Agent": "evo-agents-marketing/1"})
        body = await self.call(HttpRequest("GET", f"{self.API}/info?id={external_id}", headers))
        kids = (body.get("data") or {}).get("children") or [{}]
        found = kids[0].get("data") or {}
        removed = 1.0 if found.get("removed_by_category") else 0.0
        return {
            "score": float(found.get("score", 0)),
            "comments": float(found.get("num_comments", 0)),
            "removed": removed,
        }

    async def replies(self, external_id: str, token: str) -> list[dict]:
        """The top comments under a post of the account (its fullname ``t3_...``), each by its fullname."""
        headers = _bearer(token, **{"User-Agent": "evo-agents-marketing/1"})
        url = f"https://oauth.reddit.com/comments/{external_id.removeprefix('t3_')}?depth=1&limit=50"
        body = await self.call(HttpRequest("GET", url, headers))
        listings = body.get("items") if isinstance(body.get("items"), list) else []
        comments = ((listings[1] if len(listings) > 1 else {}).get("data") or {}).get("children") or []
        found = []
        for child in (child for child in comments if isinstance(child, dict)):
            data = child.get("data") or {}
            if child.get("kind") == "t1" and data.get("name") and data.get("body"):
                url = f"https://www.reddit.com{data['permalink']}" if data.get("permalink") else None
                found.append(
                    {"external_id": data["name"], "author": data.get("author"), "url": url, "body": data["body"]}
                )
        return found


class FacebookPage(_Base):
    """A post on a Page (target is the page id). Posting only: no feed and no insight is read."""

    channel = "facebook_page"
    API = "https://graph.facebook.com/v19.0"

    async def publish(self, content: Content, token: str) -> Published:
        if not content.target:
            raise AdapterError("missing", "facebook_page: the account has no target page id")
        body = await self.call(
            HttpRequest("POST", f"{self.API}/{content.target}/feed", _bearer(token), {"message": content.body})
        )
        found = str(body.get("id", ""))
        return Published(f"https://www.facebook.com/{found}", found)

    async def health(self, target: str | None, token: str) -> str:
        """The page read with the credential, whose tasks must let it create content. Reads no feed and no insight."""
        if not target:
            raise AdapterError("missing", "facebook_page: the account has no target page id")
        url = f"{self.API}/{target}?fields=name,tasks"
        body = await self.read(HttpRequest("GET", url, _bearer(token)))
        if "CREATE_CONTENT" not in (body.get("tasks") or []):
            raise AdapterError("rejected", f"facebook_page: the credential reads page {target} but may not post on it")
        return f"may post on {body.get('name') or target}"


class TikTok(_Base):
    """A photo post by the Content Posting API from a media url the post links to. Posting only."""

    channel = "tiktok"
    API = "https://open.tiktokapis.com/v2/post/publish/content/init/"

    async def publish(self, content: Content, token: str) -> Published:
        if not content.media_url:
            raise AdapterError("missing", "tiktok: the post links to no image to publish")
        payload = {
            "post_info": {"title": (content.title or content.body)[:90], "description": content.body},
            "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": [content.media_url]},
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
        }
        body = await self.call(HttpRequest("POST", self.API, _bearer(token), payload))
        found = str((body.get("data") or {}).get("publish_id", ""))
        return Published(f"https://www.tiktok.com/@{content.account}", found)

    async def health(self, target: str | None, token: str) -> str:
        """The creator info query of the Content Posting API: who the credential posts as. Posts nothing."""
        url = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
        body = await self.read(HttpRequest("POST", url, _bearer(token), {}))
        code = (body.get("error") or {}).get("code") or "ok"
        creator = body.get("data") or {}
        if code != "ok" or not creator.get("creator_username"):
            raise AdapterError("rejected", f"tiktok: the creator info query answered {str(code)[:60]}")
        return f"posting as @{creator['creator_username']}"


ADAPTERS = {cls.channel: cls for cls in (GitHub, X, LinkedIn, Reddit, FacebookPage, TikTok)}

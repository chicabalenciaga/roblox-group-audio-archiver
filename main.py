import json
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple
import requests

GROUP_ID = int(os.environ.get("ROBLOX_GROUP_ID", "648972497"))
ROBLOX_COOKIE = os.environ.get("ROBLOX_COOKIE", "").strip()
ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state/tracked_audios.json")
SEED_EXISTING = os.environ.get("SEED_EXISTING", "false").lower() in ("1", "true", "yes")
REQUEST_TIMEOUT = 25

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "MaleficioGroupAudioArchiver/1.0",
    "Accept": "application/json",
})
if ROBLOX_COOKIE:
    SESSION.cookies.set(".ROBLOSECURITY", ROBLOX_COOKIE, domain=".roblox.com", path="/")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_csrf_token() -> Optional[str]:
    base = "https://" + "apis.roblox.com/assets/v1/assets"
    legacy = "https://" + "develop.roblox.com/v1/assets/1/archive"
    for url in (base, legacy):
        try:
            resp = SESSION.post(url, timeout=REQUEST_TIMEOUT)
            token = resp.headers.get("x-csrf-token")
            if token:
                return token
        except requests.RequestException:
            continue
    return None


def roblox_request(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    retry_csrf: bool = True,
) -> requests.Response:
    headers: Dict[str, str] = {}
    if ROBLOX_API_KEY:
        headers["x-api-key"] = ROBLOX_API_KEY

    if method.upper() in ("POST", "PATCH", "PUT", "DELETE"):
        token = get_csrf_token()
        if token:
            headers["x-csrf-token"] = token

    resp = SESSION.request(
        method=method.upper(),
        url=url,
        params=params,
        json=json_body,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    if resp.status_code == 403 and retry_csrf and "x-csrf-token" in resp.headers:
        SESSION.headers["x-csrf-token"] = resp.headers["x-csrf-token"]
        resp = SESSION.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

    return resp


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"group_id": GROUP_ID, "seeded": False, "audios": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "audios" not in data or not isinstance(data["audios"], dict):
        data["audios"] = {}
    data.setdefault("group_id", GROUP_ID)
    data.setdefault("seeded", False)
    return data


def save_state(path: str, state: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def fetch_group_audios(group_id: int) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    creations_url = "https://" + "itemconfiguration.roblox.com/v1/creations/get-assets"
    inventory_url = f"https://inventory.roblox.com/v2/users/{group_id}/inventory"

    cursor = ""
    page_count = 0
    while page_count < 100:
        page_count += 1
        params: Dict[str, Any] = {
            "assetType": "Audio",
            "groupId": group_id,
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor

        resp = roblox_request("GET", creations_url, params=params)
        if resp.status_code != 200:
            log(f"get-assets returned {resp.status_code}: {resp.text[:250]}")
            break

        payload = resp.json()
        items = payload.get("data") or payload.get("items") or []
        if not items:
            break

        for item in items:
            asset_id = item.get("assetId") or item.get("id")
            if asset_id is None:
                continue
            sid = str(asset_id)
            found[sid] = {
                "id": int(asset_id),
                "name": item.get("name") or f"Audio {asset_id}",
            }

        cursor = payload.get("nextPageCursor") or ""
        if not cursor:
            break

    if found:
        return found

    cursor = ""
    page_count = 0
    while page_count < 100:
        page_count += 1
        params = {
            "assetTypes": "Audio",
            "limit": 100,
            "sortOrder": "Asc",
        }
        if cursor:
            params["cursor"] = cursor

        resp = roblox_request("GET", inventory_url, params=params)
        if resp.status_code != 200:
            log(f"inventory fallback returned {resp.status_code}: {resp.text[:250]}")
            break

        payload = resp.json()
        items = payload.get("data") or []
        if not items:
            break

        for item in items:
            asset_id = item.get("assetId") or item.get("id")
            if asset_id is None:
                continue
            sid = str(asset_id)
            found[sid] = {
                "id": int(asset_id),
                "name": item.get("name") or f"Audio {asset_id}",
            }

        cursor = payload.get("nextPageCursor") or ""
        if not cursor:
            break

    return found


def check_asset_status(asset_id: int) -> Tuple[bool, str]:
    details_url = f"https://economy.roblox.com/v2/assets/{asset_id}/details"
    resp = roblox_request("GET", details_url)
    if resp.status_code == 404:
        return True, "Asset details returned 404 (deleted or removed)"
    if resp.status_code == 403:
        return True, "Asset details returned 403 (restricted or moderated)"

    if resp.status_code == 200:
        data = resp.json()
        if data.get("IsModerated") is True or data.get("isModerated") is True:
            return True, "Asset flagged as moderated"
        if data.get("IsArchived") is True or data.get("isArchived") is True:
            return False, "Already archived"

    if ROBLOX_API_KEY:
        oc_url = "https://" + f"apis.roblox.com/assets/v1/assets/{asset_id}"
        oc_resp = roblox_request("GET", oc_url)
        if oc_resp.status_code == 404:
            return True, "Open Cloud asset returned 404"
        if oc_resp.status_code == 200:
            oc_data = oc_resp.json()
            moderation = oc_data.get("moderationResult") or {}
            state = str(moderation.get("moderationState", "")).upper()
            if state in ("REJECTED", "MODERATED", "DELETED", "REVIEW_FAILED"):
                return True, f"Open Cloud moderationState={state}"

    return False, "Still active"


def archive_asset(asset_id: int) -> Tuple[bool, str]:
    archive_url = "https://" + f"apis.roblox.com/assets/v1/assets/{asset_id}:archive"
    resp = roblox_request("POST", archive_url)
    if resp.status_code in (200, 202, 204):
        return True, f"Archived via Assets API ({resp.status_code})"

    legacy_url = "https://" + f"develop.roblox.com/v1/assets/{asset_id}/archive"
    legacy_resp = roblox_request("POST", legacy_url)
    if legacy_resp.status_code in (200, 202, 204):
        return True, f"Archived via Develop API ({legacy_resp.status_code})"

    return (
        False,
        f"Archive failed (Assets API: {resp.status_code} {resp.text[:140]} | Develop API: {legacy_resp.status_code} {legacy_resp.text[:140]})",
    )


def send_discord_alarm(
    asset_id: int,
    asset_name: str,
    reason: str,
    archived_ok: bool,
    archive_detail: str,
) -> None:
    if not DISCORD_WEBHOOK_URL:
        log("DISCORD_WEBHOOK_URL is not configured; skipping alarm.")
        return

    status_label = "Archived" if archived_ok else "Archive Failed"
    color = 0xE74C3C if archived_ok else 0xF39C12

    embed = {
        "title": "Roblox Group Audio Deleted Alert",
        "color": color,
        "fields": [
            {"name": "Group ID", "value": f"`{GROUP_ID}`", "inline": True},
            {"name": "Audio ID", "value": f"`{asset_id}`", "inline": True},
            {"name": "Archive Action", "value": f"**{status_label}**", "inline": True},
            {"name": "Audio Name", "value": asset_name[:256] or "Unknown", "inline": False},
            {"name": "Detection Reason", "value": reason[:512], "inline": False},
            {"name": "Archive Details", "value": archive_detail[:512], "inline": False},
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    payload = {
        "content": f"@here Group audio `{asset_id}` (**{asset_name}**) in group `{GROUP_ID}` was detected as deleted.",
        "embeds": [embed],
    }

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code not in (200, 204):
            log(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
        else:
            log(f"Sent Discord alarm for audio {asset_id}.")
    except requests.RequestException as exc:
        log(f"Error sending Discord webhook: {exc}")


def run_once() -> None:
    if not ROBLOX_COOKIE and not ROBLOX_API_KEY:
        log("ERROR: Neither ROBLOX_COOKIE nor ROBLOX_API_KEY is set.")
        sys.exit(1)

    state = load_state(STATE_FILE)
    tracked: Dict[str, Any] = state["audios"]

    log(f"Fetching audios for Roblox group {GROUP_ID}...")
    current_audios = fetch_group_audios(GROUP_ID)
    log(f"Fetched {len(current_audios)} audio entries from group {GROUP_ID}.")

    if not state.get("seeded") and not tracked:
        for sid, info in current_audios.items():
            tracked[sid] = {
                "id": info["id"],
                "name": info["name"],
                "status": "active",
                "archived": False,
                "first_seen": int(time.time()),
                "last_seen": int(time.time()),
            }
        state["seeded"] = True
        save_state(STATE_FILE, state)
        log(f"Seeded {len(tracked)} existing group audios into {STATE_FILE}.")
        if not SEED_EXISTING:
            return

    now_ts = int(time.time())

    for sid, info in current_audios.items():
        entry = tracked.get(sid)
        if entry:
            entry["name"] = info["name"]
            entry["last_seen"] = now_ts
            if entry.get("status") == "deleted":
                entry["status"] = "active"
        else:
            tracked[sid] = {
                "id": info["id"],
                "name": info["name"],
                "status": "active",
                "archived": False,
                "first_seen": now_ts,
                "last_seen": now_ts,
            }

    for sid, entry in list(tracked.items()):
        if sid in current_audios:
            continue
        if entry.get("archived") is True:
            continue

        asset_id = int(entry.get("id", sid))
        asset_name = str(entry.get("name", f"Audio {asset_id}"))

        is_deleted, reason = check_asset_status(asset_id)
        if not is_deleted:
            continue

        log(f"Detected deleted/unavailable audio {asset_id} ({asset_name}): {reason}")
        archived_ok, archive_detail = archive_asset(asset_id)
        log(f"Archive result for {asset_id}: {archived_ok} ({archive_detail})")

        entry["status"] = "deleted"
        entry["archived"] = archived_ok
        entry["deleted_at"] = now_ts
        entry["delete_reason"] = reason
        entry["archive_detail"] = archive_detail

        send_discord_alarm(
            asset_id=asset_id,
            asset_name=asset_name,
            reason=reason,
            archived_ok=archived_ok,
            archive_detail=archive_detail,
        )

    save_state(STATE_FILE, state)
    log("Cycle finished and state saved.")


if __name__ == "__main__":
    run_once()

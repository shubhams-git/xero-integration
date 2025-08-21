"""

auth_token_generator.py



Hardened token generator for Xero API using OAuth2 Client Credentials.

- Prefers XERO_CLIENT_ID / XERO_CLIENT_SECRET from the existing .env

- Never overwrites those two keys when saving

- Updates only XERO_CLIENT_BEARER_TOKEN (+ metadata)

- Atomic .env writes with backup and permission preservation

"""



import argparse

import base64

import json

import logging

import os

import stat

import sys

import tempfile

from datetime import datetime, timedelta

from getpass import getpass

from pathlib import Path

from typing import Dict, List, Optional, Tuple



import requests



# ------------ Logging ------------

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s - %(levelname)s - %(message)s",

)

logger = logging.getLogger("xero-token")



# ------------ Config ------------

TOKEN_ENDPOINT = "https://identity.xero.com/connect/token"

DEFAULT_SCOPES = [

    "accounting.transactions",

    "accounting.contacts",

    "accounting.settings",

    "accounting.reports.read",

]



ENV_KEY_ID = "XERO_CLIENT_ID"

ENV_KEY_SECRET = "XERO_CLIENT_SECRET"

ENV_KEY_TOKEN = "XERO_CLIENT_BEARER_TOKEN"



# ------------ Exceptions ------------

class TokenGeneratorError(Exception):

    pass



# ------------ .env helpers (no external deps) ------------

def parse_env_line(line: str) -> Optional[Tuple[str, str, str]]:

    """

    Parse a line like KEY=VALUE (supports quotes). Returns (key, value, sep)

    sep is the exact separator including '=' and any spaces so we can rewrite in place.

    Returns None if not a simple assignment line.

    """

    stripped = line.lstrip()

    if not stripped or stripped.startswith("#"):

        return None

    # very simple parser: KEY[spaces]=[spaces]VALUE

    if "=" not in line:

        return None

    before, after = line.split("=", 1)

    key = before.strip()

    if not key or " " in key or "\t" in key:

        return None

    sep = line[len(before):len(before)+1]  # the '='

    # Preserve any spaces around '=' in sep_ext

    # Recompute sep with spaces to keep original formatting

    # Find index of '=' in original line

    eq_idx = line.find("=")

    sep_ext = line[len(before): eq_idx + 1]

    value = after.rstrip("\n")

    return key, value, sep_ext



def load_env_file(env_path: Path) -> Tuple[List[str], Dict[str, str]]:

    """

    Returns (lines, mapping). mapping is last assignment wins for duplicates.

    Values are raw (no quote unwrapping).

    """

    if not env_path.exists():

        return [], {}

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    mapping: Dict[str, str] = {}

    for line in lines:

        parsed = parse_env_line(line)

        if parsed:

            key, value, _ = parsed

            mapping[key] = value

    return lines, mapping



def write_env_atomic(env_path: Path, new_lines: List[str]) -> None:

    """

    Write atomically and preserve original file permissions if present.

    """

    perm = None

    if env_path.exists():

        st = env_path.stat()

        perm = stat.S_IMODE(st.st_mode)



    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=str(env_path.parent)) as tmp:

        tmp.write("".join(new_lines))

        tmp_path = Path(tmp.name)



    if perm is not None:

        os.chmod(tmp_path, perm)



    # Backup

    if env_path.exists():

        backup = env_path.with_suffix(env_path.suffix + f".backup.{int(datetime.now().timestamp())}")

        env_path.replace(backup)

        logger.info(f"Backed up {env_path} to {backup.name}")



    tmp_path.replace(env_path)



def upsert_env_key(lines: List[str], key: str, value: str) -> List[str]:

    """

    Update key if exists (first match), else append. Preserve formatting where possible.

    """

    updated = False

    out: List[str] = []

    for line in lines:

        parsed = parse_env_line(line)

        if parsed:

            k, v, sep = parsed

            if k == key and not updated:

                # normalize to KEY=VALUE (no added quotes)

                out.append(f"{k}{sep}{value}\n")

                updated = True

            else:

                out.append(line)

        else:

            out.append(line)

    if not updated:

        # Ensure nice spacing and newline between blocks

        if out and not out[-1].endswith("\n"):

            out[-1] = out[-1] + "\n"

        out.append(f"{key}={value}\n")

    return out



def ensure_preserved_keys(

    orig_lines: List[str],

    preserved: Dict[str, str],

) -> List[str]:

    """

    Guarantee preserved keys keep their original values if they already existed.

    If a preserved key didn't exist, optionally add it using provided value (if not None).

    """

    # Determine existing values from original lines

    existing: Dict[str, str] = {}

    for line in orig_lines:

        parsed = parse_env_line(line)

        if parsed:

            k, v, _ = parsed

            existing[k] = v



    out = orig_lines[:]



    for k, v in preserved.items():

        if k in existing:

            # Re-write to the exact existing value (preserve userâ€™s)

            out = upsert_env_key(out, k, existing[k])

        elif v is not None:

            # Add only if not present at all

            out = upsert_env_key(out, k, v)

        # If v is None and key not present, do nothing

    return out



def inject_token_block(lines: List[str], token: str, expires_at_iso: Optional[str]) -> List[str]:

    """

    Writes/updates XERO_CLIENT_BEARER_TOKEN and a compact metadata header.

    Header is updated idempotently (won't duplicate per run).

    """

    # First, upsert the token variable

    out = upsert_env_key(lines, ENV_KEY_TOKEN, token)



    # Now manage a one-line metadata comment right above the token line

    # Strategy: remove any existing "# XERO TOKEN:" lines, then insert a fresh one above the token assignment.

    pruned: List[str] = []

    for line in out:

        if line.startswith("# XERO TOKEN:"):

            continue

        pruned.append(line)

    out = pruned



    # Re-find the token line index

    idx = next((i for i, l in enumerate(out) if l.lstrip().startswith(f"{ENV_KEY_TOKEN}=")), None)

    meta = f"# XERO TOKEN: generated {datetime.now().isoformat()} AEST; expires {expires_at_iso or 'unknown'}\n"

    if idx is None:

        # token line not found (unexpected because upsert added it). Append meta then token for safety.

        out.append(meta)

        out.append(f"{ENV_KEY_TOKEN}={token}\n")

    else:

        out.insert(idx, meta)

    return out



# ------------ Token generator ------------

class XeroTokenGenerator:

    def __init__(self, client_id: str, client_secret: str):

        self.client_id = client_id.strip()

        self.client_secret = client_secret.strip()

        if not self.client_id or not self.client_secret:

            raise TokenGeneratorError("Client ID and Secret are required.")

        if len(self.client_id) < 8 or len(self.client_secret) < 8:

            raise TokenGeneratorError("Client ID/Secret look too short; double-check.")



    def _basic_auth_header(self) -> str:

        b = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8"))

        return "Basic " + b.decode("utf-8")



    def generate_token(self, scopes: Optional[List[str]] = None) -> Dict:

        scopes = scopes or DEFAULT_SCOPES

        payload = {

            "grant_type": "client_credentials",

            "scope": " ".join(scopes),

        }

        headers = {

            "Content-Type": "application/x-www-form-urlencoded",

            "Authorization": self._basic_auth_header(),

            "User-Agent": "XeroMCPClient/1.1",

        }

        try:

            resp = requests.post(TOKEN_ENDPOINT, data=payload, headers=headers, timeout=30)

        except requests.RequestException as e:

            raise TokenGeneratorError(f"Network error: {e}") from e



        if resp.status_code != 200:

            try:

                data = resp.json()

                code = data.get("error", f"http_{resp.status_code}")

                desc = data.get("error_description", resp.text)

            except json.JSONDecodeError:

                code = f"http_{resp.status_code}"

                desc = resp.text

            raise TokenGeneratorError(f"Token request failed: {code} - {desc}")



        token = resp.json()

        if "access_token" not in token:

            raise TokenGeneratorError(f"Invalid token response: {token}")

        if "expires_in" in token:

            token["expires_at"] = (datetime.now() + timedelta(seconds=int(token["expires_in"]))).isoformat()

        return token



    def validate_token(self, access_token: str) -> bool:

        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        try:

            r = requests.get("https://api.xero.com/api.xro/2.0/Organisations", headers=headers, timeout=15)

            return r.status_code == 200

        except requests.RequestException as e:

            logger.warning(f"Validation network error: {e}")

            return False



# ------------ CLI ------------

def resolve_credentials(env_path: Path) -> Tuple[str, str, Dict[str, str], List[str]]:

    """

    Load ID/SECRET from .env if present; else from process env; else prompt.

    Returns (client_id, client_secret, env_map, env_lines)

    """

    env_lines, env_map = load_env_file(env_path)



    cid = env_map.get(ENV_KEY_ID)

    csec = env_map.get(ENV_KEY_SECRET)



    if cid and csec:

        logger.info(f"Using credentials from {env_path}")

    else:

        # Fall back to process env

        cid = cid or os.getenv(ENV_KEY_ID)

        csec = csec or os.getenv(ENV_KEY_SECRET)



    if not cid:

        cid = input("Enter Xero Client ID: ").strip()

    if not csec:

        csec = getpass("Enter Xero Client Secret (input hidden): ").strip()



    if not cid or not csec:

        raise TokenGeneratorError("Client ID and Secret are required.")



    return cid, csec, env_map, env_lines



def main():

    ap = argparse.ArgumentParser(description="Xero OAuth2 Client Credentials token generator")

    ap.add_argument("--env-file", default=".env", help="Path to .env (default: ./.env)")

    ap.add_argument("--scopes", nargs="*", default=None, help="Override scopes (space-separated)")

    ap.add_argument("--no-validate", action="store_true", help="Skip validation call")

    ap.add_argument("--print-token", action="store_true", help="Print raw token to stdout")

    args = ap.parse_args()



    env_path = Path(args.env_file)



    try:

        client_id, client_secret, env_map, env_lines = resolve_credentials(env_path)

        gen = XeroTokenGenerator(client_id, client_secret)



        scopes = args.scopes or DEFAULT_SCOPES

        logger.info("Requesting OAuth2 bearer token from Xero")

        logger.info(f"Scopes: {' '.join(scopes)}")



        token_response = gen.generate_token(scopes=scopes)

        access_token = token_response["access_token"]

        expires_at = token_response.get("expires_at")



        if args.print_token:

            print(f"\nAccess Token:\n{access_token}\n")

        else:

            masked = access_token[:8] + "..." + access_token[-8:]

            print(f"\nToken generated (masked): {masked}")

        print(f"Token type: {token_response.get('token_type', 'Bearer')}")

        print(f"Expires in: {token_response.get('expires_in', 'unknown')}s")

        if expires_at:

            print(f"Expires at: {expires_at}")



        if not args.no_validate:

            valid = gen.validate_token(access_token)

            print("Validation:", "âœ… OK" if valid else "âš ï¸  Failed (token may still be valid for other endpoints)")



        # --- Save to .env, preserving ID/SECRET exactly as they were ---

        # Prepare preserved values:

        # - If .env already had those keys, keep them AS-IS.

        # - If not present, add them using the ones we just used (so future runs donâ€™t prompt).

        preserved = {

            ENV_KEY_ID: env_map.get(ENV_KEY_ID) if ENV_KEY_ID in env_map else client_id,

            ENV_KEY_SECRET: env_map.get(ENV_KEY_SECRET) if ENV_KEY_SECRET in env_map else client_secret,

        }



        post_preserve = ensure_preserved_keys(env_lines, preserved)

        final_lines = inject_token_block(post_preserve, token=access_token, expires_at_iso=expires_at)



        write_env_atomic(env_path, final_lines)

        logger.info(f"âœ… Updated {env_path} (kept {ENV_KEY_ID}/{ENV_KEY_SECRET} intact)")



        print("\nDone. Environment updated:")

        print(f"  {env_path} now contains {ENV_KEY_TOKEN} and metadata.")

        print(f"  {ENV_KEY_ID}/{ENV_KEY_SECRET} were preserved untouched.")



    except TokenGeneratorError as e:

        logger.error(f"Token generation failed: {e}")

        print(f"âŒ Error: {e}")

        print("\nTroubleshooting:")

        print("  â€¢ Confirm Client ID/Secret (from Xero developer portal)")

        print("  â€¢ Ensure your app is enabled for client credentials + scopes")

        print("  â€¢ Check network / proxies / TLS interception")

        sys.exit(1)

    except KeyboardInterrupt:

        print("\nâ¹ï¸  Cancelled by user")

        sys.exit(130)

    except Exception as e:

        logger.error("Unexpected error", exc_info=True)

        print(f"ðŸ’¥ Unexpected error: {e}")

        sys.exit(1)



if __name__ == "__main__":

    main()
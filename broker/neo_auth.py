"""
Kotak Neo Authentication Manager.
Provides zero-touch automated 2FA login (via pyotp) or interactive TOTP entry,
with full daily session token caching in data/sessions/neo_session.json.
"""
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
import pyotp

from config.settings import KotakNeoCredentials
from core.timeutils import today_ist_str, now_ist

logger = logging.getLogger("KotakNeoAuth")


class KotakNeoAuth:
    """
    Handles Kotak Neo API authentication, automatic/manual TOTP validation,
    and daily session caching.
    """
    def __init__(
        self,
        credentials: KotakNeoCredentials,
        session_file: Optional[Path] = None
    ):
        self.creds = credentials
        if session_file is None:
            self.session_file = Path("data/sessions/neo_session.json")
        else:
            self.session_file = Path(session_file)
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.client = None

    def load_cached_session(self) -> Optional[Dict[str, Any]]:
        """Loads session file if valid for today's trading day."""
        if not self.session_file.exists():
            return None

        try:
            with open(self.session_file, "r") as f:
                data = json.load(f)

            saved_date = data.get("session_date")
            if saved_date == today_ist_str() and data.get("token") and data.get("sid"):
                logger.info(f"Loaded existing Kotak Neo session for date {saved_date}")
                return data
            else:
                logger.info(f"Cached session from {saved_date} is expired (Today: {today_ist_str()})")
                return None
        except Exception as e:
            logger.warning(f"Error reading session file: {e}")
            return None

    def save_session(self, session_data: Dict[str, Any]):
        session_data["session_date"] = today_ist_str()
        session_data["saved_at_ist"] = now_ist().isoformat()
        try:
            temp_file = self.session_file.with_suffix(".tmp")
            with open(temp_file, "w") as f:
                json.dump(session_data, f, indent=2)
            temp_file.replace(self.session_file)
            logger.info(f"Kotak Neo session saved successfully to {self.session_file}")
        except Exception as e:
            logger.error(f"Failed to save session file: {e}")

    def generate_totp(self) -> Optional[str]:
        """Generates dynamic 6-digit TOTP code using the stored base32 secret."""
        if not self.creds.totp_secret:
            return None
        try:
            totp = pyotp.TOTP(self.creds.totp_secret.replace(" ", "").upper())
            return totp.now()
        except Exception as e:
            logger.warning(f"Failed to generate TOTP from secret: {e}")
            return None

    def get_authenticated_client(self, force_login: bool = False):
        """
        Authenticates with Kotak Neo and returns the initialized NeoAPI client.
        Uses cached session if available; otherwise performs TOTP login.
        """
        try:
            from neo_api_client import NeoAPI
        except ImportError:
            raise ImportError(
                "neo_api_client is not installed. Please install using: "
                "pip install git+https://github.com/Kotak-Neo/kotak-neo-python.git"
            )

        if not self.creds.consumer_key:
            logger.warning("KOTAK_NEO_CONSUMER_KEY (API Access Token) not configured.")
            return None

        client = NeoAPI(
            consumer_key=self.creds.consumer_key,
            environment=self.creds.environment or "prod"
        )

        cached = None if force_login else self.load_cached_session()
        if cached:
            # Restore cached session attributes onto client configuration
            try:
                client.configuration.edit_token = cached.get("token")
                client.configuration.edit_sid = cached.get("sid")
                client.configuration.edit_rid = cached.get("rid")
                client.configuration.data_center = cached.get("dataCenter")
                client.configuration.base_url = cached.get("baseUrl")
                client.configuration.ucc = cached.get("ucc") or self.creds.ucc
                client.configuration.feed_url = cached.get("feedUrl")
                client.configuration.rt_url = cached.get("rtUrl")

                # Test validity with limits call
                test_res = client.limits()
                if isinstance(test_res, dict) and "error" not in test_res and "Error" not in test_res:
                    logger.info("✅ Cached Kotak Neo session validated successfully!")
                    self.client = client
                    return client
                else:
                    logger.warning(f"Cached session validation rejected by broker, logging in fresh...")
            except Exception as e:
                logger.warning(f"Cached session restoration error ({e}), logging in fresh...")

        # Initiate Login Flow
        logger.info("🔐 Initiating Kotak Neo 2FA authentication...")
        
        if not self.creds.mobile_number or not self.creds.ucc or not self.creds.mpin:
            logger.error(
                "Missing credentials in .env. Required: "
                "KOTAK_NEO_CONSUMER_KEY (API Access Token), KOTAK_NEO_MOBILE_NUMBER, KOTAK_NEO_UCC, KOTAK_NEO_MPIN"
            )
            return None

        # Determine TOTP
        current_totp = self.generate_totp()
        if current_totp:
            logger.info("Generated fresh TOTP code automatically via pyotp.")
        else:
            # Interactive prompt fallback
            print("\n" + "="*50)
            print("📲 KOTAK NEO AUTHENTICATION")
            print("="*50)
            print("KOTAK_NEO_TOTP_SECRET is not set in .env.")
            print("Please open your phone's Authenticator App (Google Authenticator / Authy)")
            print("and enter the 6-digit TOTP code for Kotak Neo below:")
            print("-" * 50)
            try:
                current_totp = input("Enter 6-digit TOTP: ").strip()
            except (EOFError, KeyboardInterrupt):
                logger.error("Login cancelled by user.")
                return None

        if not current_totp or len(current_totp) != 6:
            logger.error(f"Invalid TOTP entered: '{current_totp}'. Must be 6 digits.")
            return None

        try:
            # Step 1: TOTP Login
            login_resp = client.totp_login(
                mobile_number=self.creds.mobile_number,
                ucc=self.creds.ucc,
                totp=current_totp
            )
            logger.debug(f"totp_login response: {login_resp}")

            if isinstance(login_resp, dict) and login_resp.get("Error"):
                logger.error(f"❌ TOTP Login failed: {login_resp.get('Error')}")
                return None

            # Step 2: Validate MPIN
            val_resp = client.totp_validate(mpin=self.creds.mpin)
            logger.debug(f"totp_validate response: {val_resp}")

            if isinstance(val_resp, dict) and val_resp.get("Error"):
                logger.error(f"❌ MPIN Validation failed: {val_resp.get('Error')}")
                return None

            session_data = val_resp.get("data") if isinstance(val_resp, dict) else {}
            if not session_data or not session_data.get("token"):
                logger.error(f"❌ Unexpected response from totp_validate: {val_resp}")
                return None

            logger.info("🎉 SUCCESS! Authenticated with Kotak Neo API.")
            
            # Save session for today
            self.save_session(session_data)
            self.client = client
            return client

        except Exception as e:
            logger.error(f"❌ Kotak Neo login exception: {e}")
            raise

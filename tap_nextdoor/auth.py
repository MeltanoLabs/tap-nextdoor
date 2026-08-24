"""Nextdoor Ads API authentication.

Access is granted via the Ads and Conversion API Request Form; the approved
account then generates a long-lived access token in Nextdoor Ads Manager
(https://ads.nextdoor.com/v2/manage/api). The docs describe the scheme as
OAuth2 with a bearer token, but since the token is issued out-of-band there is
no client-credentials or refresh-token flow for the tap to perform - it just
presents the token. If Nextdoor later requires a full OAuth flow, replace this
with an ``OAuthAuthenticator`` subclass:
https://sdk.meltano.com/en/latest/authenticators.html
"""

from __future__ import annotations

from singer_sdk.authenticators import APIKeyAuthenticator


class NextdoorAuthenticator(APIKeyAuthenticator):
    """Sends the access token as an ``Authorization: Bearer <token>`` header."""

    @classmethod
    def create_for_tap(cls, access_token: str) -> NextdoorAuthenticator:
        """Instantiate an authenticator for the configured access token.

        Args:
            access_token: The NAM API access token.

        Returns:
            A new authenticator.
        """
        return cls(
            key="Authorization",
            value=f"Bearer {access_token}",
            location="header",
        )

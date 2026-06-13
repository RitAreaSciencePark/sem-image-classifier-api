"""Configure Authentik OAuth2 provider for machine-to-machine API access.

Creates a confidential OAuth2 provider with RS256 signing for KrakenD JWKS
validation and client_credentials grant for API tokens.

Required env vars:
- OIDC_CLIENT_SECRET
- AUTHENTIK_BOOTSTRAP_PASSWORD

Optional env vars:
- OIDC_CLIENT_ID (default: sem-classifier-api)
- OIDC_APPLICATION_SLUG (default: sem-classifier)
"""

import os
import sys

import django

sys.path.append("/")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application, User
from authentik.crypto.models import CertificateKeyPair
from authentik.flows.models import Flow, FlowStageBinding
from authentik.providers.oauth2.models import ClientTypes, OAuth2Provider, ScopeMapping


def configure():
    print("Starting automated Authentik M2M configuration...")

    provider_name = os.environ.get("OIDC_PROVIDER_NAME", "model-api-provider")
    app_name = os.environ.get("OIDC_APP_DISPLAY_NAME", "Model API")
    app_slug = os.environ.get("OIDC_APPLICATION_SLUG", "model-api")
    client_id = os.environ.get("OIDC_CLIENT_ID", "model-api-client")
    client_secret = os.environ.get("OIDC_CLIENT_SECRET", "")
    if not client_secret:
        print("Error: OIDC_CLIENT_SECRET is required")
        return False

    key = CertificateKeyPair.objects.filter(
        name="authentik Self-signed Certificate"
    ).first()
    if not key:
        print("Error: 'authentik Self-signed Certificate' not found.")
        return False
    print(f"Found signing key: {key.name}")

    flow = Flow.objects.filter(
        slug="default-provider-authorization-implicit-consent"
    ).first()
    if not flow:
        flow = Flow.objects.filter(
            slug="default-provider-authorization-explicit-consent"
        ).first()
    if not flow:
        print("Error: Could not find a default authorization flow.")
        return False
    print(f"Found authorization flow: {flow.slug}")

    provider, created = OAuth2Provider.objects.update_or_create(
        name=provider_name,
        defaults={
            "client_id": client_id,
            "client_secret": client_secret,
            "client_type": ClientTypes.CONFIDENTIAL,
            "redirect_uris": "",
            "signing_key": key,
            "authorization_flow": flow,
        },
    )

    default_mappings = ScopeMapping.objects.filter(
        managed__startswith="goauthentik.io/providers/oauth2/scope-"
    )
    provider.property_mappings.set(default_mappings)

    action = "Created" if created else "Updated"
    print(f"{action} OAuth2 Provider: {provider.name}")
    print(f"  Client ID: {provider.client_id}")
    print(f"  Scopes: {', '.join(m.scope_name for m in default_mappings)}")
    print(f"  JWKS path: /application/o/{app_slug}/jwks/")

    app, app_created = Application.objects.update_or_create(
        slug=app_slug,
        defaults={
            "name": app_name,
            "provider": provider,
            "meta_launch_url": "",
            "open_in_new_tab": False,
        },
    )
    action = "Created" if app_created else "Updated"
    print(f"{action} Application: {app.name} (slug={app_slug})")

    auth_flow = Flow.objects.filter(slug="default-authentication-flow").first()
    if auth_flow:
        mfa_bindings = FlowStageBinding.objects.filter(
            target=auth_flow,
            stage__name="default-authentication-mfa-validation",
        )
        if mfa_bindings.exists():
            mfa_bindings.delete()
            print("Removed MFA validation stage from authentication flow")

    bootstrap_password = os.environ.get("AUTHENTIK_BOOTSTRAP_PASSWORD", "")
    if not bootstrap_password:
        print("Error: AUTHENTIK_BOOTSTRAP_PASSWORD is required")
        return False
    try:
        admin = User.objects.get(username="akadmin")
        admin.set_password(bootstrap_password)
        admin.save()
        print("Reset akadmin password")
    except User.DoesNotExist:
        print("Warning: akadmin user not found")

    print("\nAuthentik M2M configuration complete!")
    return True


if __name__ == "__main__":
    success = configure()
    if not success:
        sys.exit(1)

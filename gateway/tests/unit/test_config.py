import pytest
from pydantic import SecretStr, ValidationError

from aiva_gateway.config import Settings


def test_anthropic_key_prefers_aiva_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIVA_ANTHROPIC_API_KEY", "aiva-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "plain-value")
    key = Settings().anthropic_api_key
    assert key is not None and key.get_secret_value() == "aiva-value"


def test_anthropic_key_falls_back_to_standard_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIVA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "plain-value")
    key = Settings().anthropic_api_key
    assert key is not None and key.get_secret_value() == "plain-value"


@pytest.mark.parametrize("environment", ["prod", "production", "PROD", "demo", "staging"])
def test_dev_sign_in_is_refused_outside_local_and_test(environment: str) -> None:
    with pytest.raises(ValidationError, match="AIVA_DEV_SIGN_IN may only be enabled"):
        Settings(environment=environment, dev_sign_in=True)


def test_dev_sign_in_is_allowed_locally_and_in_tests() -> None:
    assert Settings(environment="local", dev_sign_in=True).dev_sign_in_allowed
    assert Settings(environment="test", dev_sign_in=True).dev_sign_in_allowed
    assert not Settings(environment="demo").dev_sign_in_allowed


@pytest.mark.parametrize("environment", ["prod", "production", "demo", "staging"])
@pytest.mark.parametrize(
    "override", [{"public_sessions_per_source": 50}, {"public_sessions_per_tenant": 1000}]
)
def test_public_session_limits_are_refused_outside_local_and_test(
    environment: str, override: dict[str, int]
) -> None:
    with pytest.raises(ValidationError, match="AIVA_PUBLIC_SESSIONS_PER_TENANT may only be changed"):
        Settings(environment=environment, **override)


def test_public_session_limits_change_only_locally_and_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(environment="demo").public_session_limits == (5, 200)
    assert Settings(environment="local", public_sessions_per_source=50).public_session_limits == (50, 200)
    monkeypatch.setenv("AIVA_PUBLIC_SESSIONS_PER_TENANT", "1000")
    assert Settings(environment="test").public_session_limits == (5, 1000)
    # A deployed gateway keeps the defaults even when the validator is bypassed.
    bypassed = Settings(environment="test").model_copy(update={"environment": "demo"})
    assert bypassed.public_session_limits == (5, 200)


def test_a_platform_proxy_gateway_needs_a_long_proxy_secret() -> None:
    with pytest.raises(ValidationError, match="AIVA_PROXY_SECRET"):
        Settings(ingress="platform-proxy")
    with pytest.raises(ValidationError, match="AIVA_PROXY_SECRET"):
        Settings(ingress="platform-proxy", proxy_secret=SecretStr("short"))
    assert Settings(ingress="platform-proxy", proxy_secret=SecretStr("p" * 32)).ingress == "platform-proxy"


def test_the_demo_passcode_has_a_minimum_length() -> None:
    with pytest.raises(ValidationError, match="AIVA_DEMO_PASSCODE"):
        Settings(demo_passcode=SecretStr("1234567"))
    assert Settings(demo_passcode=SecretStr("x" * 16)).demo_passcode is not None


def test_a_settings_error_never_prints_secrets_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only canaries in the environment, so even a failing assertion cannot print a real secret.
    for name in ("AIVA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AIVA_PSEUDONYM_KEY"):
        monkeypatch.delenv(name, raising=False)
    canary = "hf_CANARYsettingsValidationLeak000"
    monkeypatch.setenv("HF_TOKEN", canary)
    monkeypatch.setenv("AIVA_JWT_SECRET", canary)
    with pytest.raises(ValidationError) as raised:
        Settings(environment="demo", dev_sign_in=True)
    # What a start-up failure prints: the message and the traceback's exception text.
    leaked = canary in str(raised.value) or canary in repr(raised.value)
    assert not leaked


def test_connection_urls_are_secrets_in_every_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only canaries in the environment, so even a failing assertion cannot print a real secret.
    for name in ("AIVA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "HF_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    passwords = ("CANARYdbPassword0", "CANARYredisPassword0", "CANARYopaPassword0")
    monkeypatch.setenv("AIVA_DATABASE_URL", f"postgresql+asyncpg://aiva:{passwords[0]}@127.0.0.1:5432/aiva")
    monkeypatch.setenv("AIVA_REDIS_URL", f"redis://:{passwords[1]}@127.0.0.1:6379/0")
    monkeypatch.setenv("AIVA_OPA_URL", f"http://opa:{passwords[2]}@127.0.0.1:8181")
    settings = Settings()
    renderings = [repr(settings), str(settings), settings.model_dump_json(), repr(settings.model_dump())]
    with pytest.raises(ValidationError) as raised:
        Settings(environment="demo", dev_sign_in=True)
    renderings += [str(raised.value), repr(raised.value)]
    leaked = [i for i, text in enumerate(renderings) if any(p in text for p in passwords)]
    assert not leaked
    # The values are intact for the code that connects.
    assert passwords[0] in settings.database_url.get_secret_value()
    assert passwords[1] in settings.redis_url.get_secret_value()
    assert passwords[2] in settings.opa_url.get_secret_value()

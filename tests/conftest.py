import pytest

from alditalk.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        aldi_phone="015712345678",
        aldi_password="hunter2-secret",
        state_dir=tmp_path,
        telegram_token=None,
        telegram_chat_id=None,
    )

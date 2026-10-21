from pathlib import Path
from unittest.mock import Mock

import pytest

from src.config.loader import Chat, load_config
from src.metrics import Metrics
from src.settings import Settings
from src.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'test.db')
    database.upsert_source(Chat(id='test', name='Test chat', chat_id=-100123, username='test_chat'))
    yield database
    database.close()


@pytest.fixture
def cfg():
    config = load_config(ROOT / 'config')
    config.chats = [Chat(id='test', name='Test chat', chat_id=-100123)]
    return config


@pytest.fixture
def settings(tmp_path):
    return Settings(api_id=1, api_hash='unused', session='', bot_token='unused', owner_chat_id=42,
                    config_dir=ROOT / 'config', data_dir=tmp_path, db_path=tmp_path / 'test.db',
                    log_level='WARNING', store_all_messages=True, jev_api_key='unused')


@pytest.fixture
def notifier():
    return Mock()


@pytest.fixture
def metrics():
    return Metrics()

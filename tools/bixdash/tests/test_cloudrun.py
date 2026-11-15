import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def server():
    path = Path(__file__).parents[1] / 'dashboard/server.py'
    spec = importlib.util.spec_from_file_location('bixdash_server', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cloudrun_listens_on_injected_port(server, tmp_path):
    with patch.dict('os.environ', {'K_SERVICE': 'bixdash', 'PORT': '8080', 'BIXDASH_TOKEN': 'test-token'}), patch('sys.argv', ['server.py', '--source', str(tmp_path)]), patch.object(server, 'ThreadingHTTPServer') as http:
        server.main()
    assert http.call_args.args[0] == ('0.0.0.0', 8080)
    http.return_value.serve_forever.assert_called_once()


def test_cloudrun_fails_closed_without_shared_token(server, tmp_path):
    with patch.dict('os.environ', {'K_SERVICE': 'bixdash', 'BIXDASH_TOKEN': ''}), patch('sys.argv', ['server.py', '--source', str(tmp_path)]):
        with pytest.raises(RuntimeError, match='BIXDASH_TOKEN is required'):
            server.main()


def test_shared_auth_rejects_missing_or_wrong_tokens(server, tmp_path):
    handler = server.make_handler(server.Source(str(tmp_path)), 'correct-token')
    request = object.__new__(handler)
    request.headers = {}
    assert not request._authorized({})
    assert not request._authorized({'token': ['wrong-token']})
    assert request._authorized({'token': ['correct-token']})
    request.headers = {'Cookie': 'bixdash=correct-token'}
    assert request._authorized({})

from collections import defaultdict
from contextlib import contextmanager
import copy
from datetime import (
    datetime,
    timedelta,
    )
import errno
import json
import logging
import os
import socket
import StringIO
import subprocess
import sys
from tempfile import NamedTemporaryFile
from textwrap import dedent
import types

from mock import (
    call,
    MagicMock,
    Mock,
    patch,
    )
import yaml

from fakejuju import (
    fake_juju_client,
    get_user_register_command_info,
    get_user_register_token,
    )
from jujuconfig import (
    get_environments_path,
    get_jenv_path,
    NoSuchEnvironment,
    )
from jujupy import (
    BootstrapMismatch,
    CannotConnectEnv,
    client_from_config,
    Controller,
    EnvJujuClient,
    EnvJujuClient1X,
    EnvJujuClient22,
    EnvJujuClient24,
    EnvJujuClient25,
    EnvJujuClientRC,
    ErroredUnit,
    GroupReporter,
    get_cache_path,
    get_local_root,
    get_machine_dns_name,
    get_timeout_path,
    IncompatibleConfigClass,
    jes_home_path,
    JESNotSupported,
    Juju2Backend,
    JujuData,
    JUJU_DEV_FEATURE_FLAGS,
    KILL_CONTROLLER,
    Machine,
    make_safe_config,
    NoProvider,
    parse_new_state_server_from_error,
    SimpleEnvironment,
    ServiceStatus,
    SoftDeadlineExceeded,
    Status,
    SYSTEM,
    tear_down,
    temp_bootstrap_env,
    _temp_env as temp_env,
    temp_yaml_file,
    uniquify_local,
    UpgradeMongoNotSupported,
    VersionNotTestedError,
    )
from tests import (
    TestCase,
    FakeHomeTestCase,
    )
from tests.test_assess_resources import make_resource_list
from utility import (
    JujuResourceTimeout,
    scoped_environ,
    temp_dir,
    )


__metaclass__ = type


def assert_juju_call(test_case, mock_method, client, expected_args,
                     call_index=None):
    if call_index is None:
        test_case.assertEqual(len(mock_method.mock_calls), 1)
        call_index = 0
    empty, args, kwargs = mock_method.mock_calls[call_index]
    test_case.assertEqual(args, (expected_args,))


class TestErroredUnit(TestCase):

    def test_output(self):
        e = ErroredUnit('bar', 'baz')
        self.assertEqual('bar is in state baz', str(e))


class ClientTest(FakeHomeTestCase):

    def setUp(self):
        super(ClientTest, self).setUp()
        patcher = patch('jujupy.pause')
        self.addCleanup(patcher.stop)
        self.pause_mock = patcher.start()


class TestTempYamlFile(TestCase):

    def test_temp_yaml_file(self):
        with temp_yaml_file({'foo': 'bar'}) as yaml_file:
            with open(yaml_file) as f:
                self.assertEqual({'foo': 'bar'}, yaml.safe_load(f))


class TestJuju2Backend(TestCase):

    test_environ = {'PATH': 'foo:bar'}

    def test_juju2_backend(self):
        backend = Juju2Backend('/bin/path', '2.0', set(), False)
        self.assertEqual('/bin/path', backend.full_path)
        self.assertEqual('2.0', backend.version)

    def test_clone_retains_soft_deadline(self):
        soft_deadline = object()
        backend = Juju2Backend('/bin/path', '2.0', feature_flags=set(),
                               debug=False, soft_deadline=soft_deadline)
        cloned = backend.clone(full_path=None, version=None, debug=None,
                               feature_flags=None)
        self.assertIsNot(cloned, backend)
        self.assertIs(soft_deadline, cloned.soft_deadline)

    def test__check_timeouts(self):
        backend = Juju2Backend('/bin/path', '2.0', set(), debug=False,
                               soft_deadline=datetime(2015, 1, 2, 3, 4, 5))
        with patch('jujupy.Juju2Backend._now',
                   return_value=backend.soft_deadline):
            with backend._check_timeouts():
                pass
        now = backend.soft_deadline + timedelta(seconds=1)
        with patch('jujupy.Juju2Backend._now', return_value=now):
            with self.assertRaisesRegexp(SoftDeadlineExceeded,
                                         'Operation exceeded deadline.'):
                with backend._check_timeouts():
                    pass

    def test__check_timeouts_no_deadline(self):
        backend = Juju2Backend('/bin/path', '2.0', set(), debug=False,
                               soft_deadline=None)
        now = datetime(2015, 1, 2, 3, 4, 6)
        with patch('jujupy.Juju2Backend._now', return_value=now):
            with backend._check_timeouts():
                pass

    def test_ignore_soft_deadline_check_timeouts(self):
        backend = Juju2Backend('/bin/path', '2.0', set(), debug=False,
                               soft_deadline=datetime(2015, 1, 2, 3, 4, 5))
        now = backend.soft_deadline + timedelta(seconds=1)
        with patch('jujupy.Juju2Backend._now', return_value=now):
            with backend.ignore_soft_deadline():
                with backend._check_timeouts():
                    pass
            with self.assertRaisesRegexp(SoftDeadlineExceeded,
                                         'Operation exceeded deadline.'):
                with backend._check_timeouts():
                    pass

    def test_juju_checks_timeouts(self):
        backend = Juju2Backend('/bin/path', '2.0', set(), debug=False,
                               soft_deadline=datetime(2015, 1, 2, 3, 4, 5))
        with patch('subprocess.check_call'):
            with patch('jujupy.Juju2Backend._now',
                       return_value=backend.soft_deadline):
                backend.juju('cmd', ('args',), [], 'home')
            now = backend.soft_deadline + timedelta(seconds=1)
            with patch('jujupy.Juju2Backend._now', return_value=now):
                with self.assertRaisesRegexp(SoftDeadlineExceeded,
                                             'Operation exceeded deadline.'):
                    backend.juju('cmd', ('args',), [], 'home')

    def test_juju_async_checks_timeouts(self):
        backend = Juju2Backend('/bin/path', '2.0', set(), debug=False,
                               soft_deadline=datetime(2015, 1, 2, 3, 4, 5))
        with patch('subprocess.Popen') as mock_popen:
            mock_popen.return_value.wait.return_value = 0
            with patch('jujupy.Juju2Backend._now',
                       return_value=backend.soft_deadline):
                with backend.juju_async('cmd', ('args',), [], 'home'):
                    pass
            now = backend.soft_deadline + timedelta(seconds=1)
            with patch('jujupy.Juju2Backend._now', return_value=now):
                with self.assertRaisesRegexp(SoftDeadlineExceeded,
                                             'Operation exceeded deadline.'):
                    with backend.juju_async('cmd', ('args',), [], 'home'):
                        pass

    def test_get_juju_output_checks_timeouts(self):
        backend = Juju2Backend('/bin/path', '2.0', set(), debug=False,
                               soft_deadline=datetime(2015, 1, 2, 3, 4, 5))
        with patch('subprocess.Popen') as mock_popen:
            mock_popen.return_value.returncode = 0
            mock_popen.return_value.communicate.return_value = ('', '')
            with patch('jujupy.Juju2Backend._now',
                       return_value=backend.soft_deadline):
                backend.get_juju_output('cmd', ('args',), [], 'home')
            now = backend.soft_deadline + timedelta(seconds=1)
            with patch('jujupy.Juju2Backend._now', return_value=now):
                with self.assertRaisesRegexp(SoftDeadlineExceeded,
                                             'Operation exceeded deadline.'):
                    backend.get_juju_output('cmd', ('args',), [], 'home')


class TestEnvJujuClient25(ClientTest):

    client_class = EnvJujuClient25

    def test_enable_jes(self):
        client = self.client_class(
            SimpleEnvironment('baz', {}),
            '1.25-foobar', 'path')
        with self.assertRaises(JESNotSupported):
            client.enable_jes()

    def test_disable_jes(self):
        client = self.client_class(
            SimpleEnvironment('baz', {}),
            '1.25-foobar', 'path')
        client.feature_flags.add('jes')
        client.disable_jes()
        self.assertNotIn('jes', client.feature_flags)

    def test_clone_unchanged(self):
        client1 = self.client_class(
            SimpleEnvironment('foo'), '1.27', 'full/path', debug=True)
        client2 = client1.clone()
        self.assertIsNot(client1, client2)
        self.assertIs(type(client1), type(client2))
        self.assertIs(client1.env, client2.env)
        self.assertEqual(client1.version, client2.version)
        self.assertEqual(client1.full_path, client2.full_path)
        self.assertIs(client1.debug, client2.debug)
        self.assertEqual(client1._backend, client2._backend)

    def test_clone_changed(self):
        client1 = self.client_class(
            SimpleEnvironment('foo'), '1.27', 'full/path', debug=True)
        env2 = SimpleEnvironment('bar')
        client2 = client1.clone(env2, '1.28', 'other/path', debug=False,
                                cls=EnvJujuClient1X)
        self.assertIs(EnvJujuClient1X, type(client2))
        self.assertIs(env2, client2.env)
        self.assertEqual('1.28', client2.version)
        self.assertEqual('other/path', client2.full_path)
        self.assertIs(False, client2.debug)

    def test_clone_defaults(self):
        client1 = self.client_class(
            SimpleEnvironment('foo'), '1.27', 'full/path', debug=True)
        client2 = client1.clone()
        self.assertIsNot(client1, client2)
        self.assertIs(self.client_class, type(client2))
        self.assertEqual(set(), client2.feature_flags)


class TestEnvJujuClient22(ClientTest):

    client_class = EnvJujuClient22

    def test__shell_environ(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.22-foobar', 'path')
        env = client._shell_environ()
        self.assertEqual(env.get(JUJU_DEV_FEATURE_FLAGS), 'actions')

    def test__shell_environ_juju_home(self):
        client = self.client_class(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.22-foobar', 'path',
            'asdf')
        env = client._shell_environ()
        self.assertEqual(env['JUJU_HOME'], 'asdf')


class TestEnvJujuClient24(ClientTest):

    client_class = EnvJujuClient24

    def test_no_jes(self):
        client = self.client_class(
            SimpleEnvironment('baz', {}),
            '1.25-foobar', 'path')
        with self.assertRaises(JESNotSupported):
            client.enable_jes()
        client._use_jes = True
        env = client._shell_environ()
        self.assertNotIn('jes', env[JUJU_DEV_FEATURE_FLAGS].split(","))

    def test_add_ssh_machines(self):
        client = self.client_class(SimpleEnvironment('foo', {}), None, 'juju')
        with patch('subprocess.check_call', autospec=True) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-bar'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-baz'), 2)
        self.assertEqual(cc_mock.call_count, 3)

    def test_add_ssh_machines_no_retry(self):
        client = self.client_class(SimpleEnvironment('foo', {}), None, 'juju')
        with patch('subprocess.check_call', autospec=True,
                   side_effect=[subprocess.CalledProcessError(None, None),
                                None, None, None]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'))


class TestTearDown(TestCase):

    def test_tear_down_no_jes(self):
        client = MagicMock()
        client.destroy_environment.return_value = 0
        tear_down(client, False)
        client.destroy_environment.assert_called_once_with(force=False)
        self.assertEqual(0, client.kill_controller.call_count)
        self.assertEqual(0, client.disable_jes.call_count)

    def test_tear_down_no_jes_exception(self):
        client = MagicMock()
        client.destroy_environment.side_effect = [1, 0]
        tear_down(client, False)
        self.assertEqual(
            client.destroy_environment.mock_calls,
            [call(force=False), call(force=True)])
        self.assertEqual(0, client.kill_controller.call_count)
        self.assertEqual(0, client.disable_jes.call_count)

    def test_tear_down_jes(self):
        client = MagicMock()
        tear_down(client, True)
        client.kill_controller.assert_called_once_with()
        self.assertEqual(0, client.destroy_environment.call_count)
        self.assertEqual(0, client.enable_jes.call_count)
        self.assertEqual(0, client.disable_jes.call_count)

    def test_tear_down_try_jes(self):

        def check_jes():
            client.enable_jes.assert_called_once_with()
            self.assertEqual(0, client.disable_jes.call_count)

        client = MagicMock()
        client.kill_controller.side_effect = check_jes

        tear_down(client, jes_enabled=False, try_jes=True)
        client.kill_controller.assert_called_once_with()
        client.disable_jes.assert_called_once_with()

    def test_tear_down_jes_try_jes(self):
        client = MagicMock()
        tear_down(client, jes_enabled=True, try_jes=True)
        client.kill_controller.assert_called_once_with()
        self.assertEqual(0, client.destroy_environment.call_count)
        self.assertEqual(0, client.enable_jes.call_count)
        self.assertEqual(0, client.disable_jes.call_count)

    def test_tear_down_try_jes_not_supported(self):

        def check_jes(force=True):
            client.enable_jes.assert_called_once_with()
            return 0

        client = MagicMock()
        client.enable_jes.side_effect = JESNotSupported
        client.destroy_environment.side_effect = check_jes

        tear_down(client, jes_enabled=False, try_jes=True)
        client.destroy_environment.assert_called_once_with(force=False)
        self.assertEqual(0, client.disable_jes.call_count)


class FakePopen(object):

    def __init__(self, out, err, returncode):
        self._out = out
        self._err = err
        self._code = returncode

    def communicate(self):
        self.returncode = self._code
        return self._out, self._err

    def poll(self):
        return self._code


@contextmanager
def observable_temp_file():
    temporary_file = NamedTemporaryFile(delete=False)
    try:
        with temporary_file as temp_file:
            with patch('jujupy.NamedTemporaryFile',
                       return_value=temp_file):
                with patch.object(temp_file, '__exit__'):
                    yield temp_file
    finally:
        try:
            os.unlink(temporary_file.name)
        except OSError as e:
            # File may have already been deleted, e.g. by temp_yaml_file.
            if e.errno != errno.ENOENT:
                raise


class TestClientFromConfig(ClientTest):

    @contextmanager
    def assertRaisesVersionNotTested(self, version):
        with self.assertRaisesRegexp(
                VersionNotTestedError, 'juju ' + version):
            yield

    @patch.object(JujuData, 'from_config', return_value=JujuData('', {}))
    @patch.object(SimpleEnvironment, 'from_config',
                  return_value=SimpleEnvironment('', {}))
    @patch.object(EnvJujuClient, 'get_full_path', return_value='fake-path')
    def test_from_config(self, gfp_mock, se_fc_mock, jd_fc_mock):
        def juju_cmd_iterator():
            yield '1.17'
            yield '1.16'
            yield '1.16.1'
            yield '1.15'
            yield '1.22.1'
            yield '1.24-alpha1'
            yield '1.24.7'
            yield '1.25.1'
            yield '1.26.1'
            yield '1.27.1'
            yield '2.0-alpha1'
            yield '2.0-alpha2'
            yield '2.0-alpha3'
            yield '2.0-beta1'
            yield '2.0-beta2'
            yield '2.0-beta3'
            yield '2.0-beta4'
            yield '2.0-beta5'
            yield '2.0-beta6'
            yield '2.0-beta7'
            yield '2.0-beta8'
            yield '2.0-beta9'
            yield '2.0-beta10'
            yield '2.0-beta11'
            yield '2.0-beta12'
            yield '2.0-beta13'
            yield '2.0-beta14'
            yield '2.0-beta15'
            yield '2.0-rc1'
            yield '2.0-rc2'
            yield '2.0-rc3'
            yield '2.0-delta1'

        context = patch.object(
            EnvJujuClient, 'get_version',
            side_effect=juju_cmd_iterator().send)
        with context:
            self.assertIs(EnvJujuClient1X,
                          type(client_from_config('foo', None)))

            def test_fc(version, cls):
                if cls is not None:
                    client = client_from_config('foo', None)
                    if isinstance(client, EnvJujuClient1X):
                        self.assertEqual(se_fc_mock.return_value, client.env)
                    else:
                        self.assertEqual(jd_fc_mock.return_value, client.env)
                    self.assertIs(cls, type(client))
                    self.assertEqual(version, client.version)
                else:
                    with self.assertRaisesVersionNotTested(version):
                        client_from_config('foo', None)

            test_fc('1.16', None)
            test_fc('1.16.1', None)
            test_fc('1.15', EnvJujuClient1X)
            test_fc('1.22.1', EnvJujuClient22)
            test_fc('1.24-alpha1', EnvJujuClient24)
            test_fc('1.24.7', EnvJujuClient24)
            test_fc('1.25.1', EnvJujuClient25)
            test_fc('1.26.1', None)
            test_fc('1.27.1', EnvJujuClient1X)
            test_fc('2.0-alpha1', None)
            test_fc('2.0-alpha2', None)
            test_fc('2.0-alpha3', None)
            test_fc('2.0-beta1', None)
            test_fc('2.0-beta2', None)
            test_fc('2.0-beta3', None)
            test_fc('2.0-beta4', None)
            test_fc('2.0-beta5', None)
            test_fc('2.0-beta6', None)
            test_fc('2.0-beta7', None)
            test_fc('2.0-beta8', None)
            test_fc('2.0-beta9', None)
            test_fc('2.0-beta10', None)
            test_fc('2.0-beta11', None)
            test_fc('2.0-beta12', None)
            test_fc('2.0-beta13', None)
            test_fc('2.0-beta14', None)
            test_fc('2.0-beta15', None)
            test_fc('2.0-rc1', EnvJujuClientRC)
            test_fc('2.0-rc2', EnvJujuClientRC)
            test_fc('2.0-rc3', EnvJujuClientRC)
            test_fc('2.0-delta1', EnvJujuClient)
            with self.assertRaises(StopIteration):
                client_from_config('foo', None)

    def test_client_from_config_path(self):
        with patch('subprocess.check_output', return_value=' 4.3') as vsn:
            with patch.object(JujuData, 'from_config'):
                client = client_from_config('foo', 'foo/bar/qux')
        vsn.assert_called_once_with(('foo/bar/qux', '--version'))
        self.assertNotEqual(client.full_path, 'foo/bar/qux')
        self.assertEqual(client.full_path, os.path.abspath('foo/bar/qux'))

    def test_client_from_config_keep_home(self):
        env = JujuData({}, juju_home='/foo/bar')
        with patch('subprocess.check_output', return_value='2.0.0'):
            with patch.object(JujuData, 'from_config',
                              side_effect=lambda x: JujuData(x, {})):
                client_from_config('foo', 'foo/bar/qux')
        self.assertEqual('/foo/bar', env.juju_home)

    def test_client_from_config_deadline(self):
        deadline = datetime(2012, 11, 10, 9, 8, 7)
        with patch('subprocess.check_output', return_value='2.0.0'):
            with patch.object(JujuData, 'from_config',
                              side_effect=lambda x: JujuData(x, {})):
                client = client_from_config(
                    'foo', 'foo/bar/qux', soft_deadline=deadline)
        self.assertEqual(client._backend.soft_deadline, deadline)


@contextmanager
def client_past_deadline():
    client = EnvJujuClient(JujuData('local', juju_home=''), None, None)
    soft_deadline = datetime(2015, 1, 2, 3, 4, 6)
    now = soft_deadline + timedelta(seconds=1)
    client._backend.soft_deadline = soft_deadline
    with patch.object(client._backend, '_now', return_value=now,
                      autospec=True):
        yield client


class TestEnvJujuClient(ClientTest):

    def test_no_duplicate_env(self):
        env = JujuData('foo', {})
        client = EnvJujuClient(env, '1.25', 'full_path')
        self.assertIs(env, client.env)

    def test_convert_to_juju_data(self):
        env = SimpleEnvironment('foo', {'type': 'bar'}, 'baz')
        with patch.object(JujuData, 'load_yaml'):
            client = EnvJujuClient(env, '1.25', 'full_path')
            client.env.load_yaml.assert_called_once_with()
        self.assertIsInstance(client.env, JujuData)
        self.assertEqual(client.env.environment, 'foo')
        self.assertEqual(client.env.config, {'type': 'bar'})
        self.assertEqual(client.env.juju_home, 'baz')

    def test_get_version(self):
        value = ' 5.6 \n'
        with patch('subprocess.check_output', return_value=value) as vsn:
            version = EnvJujuClient.get_version()
        self.assertEqual('5.6', version)
        vsn.assert_called_with(('juju', '--version'))

    def test_get_version_path(self):
        with patch('subprocess.check_output', return_value=' 4.3') as vsn:
            EnvJujuClient.get_version('foo/bar/baz')
        vsn.assert_called_once_with(('foo/bar/baz', '--version'))

    def test_get_matching_agent_version(self):
        client = EnvJujuClient(
            JujuData(None, {'type': 'local'}, juju_home='foo'),
            '1.23-series-arch', None)
        self.assertEqual('1.23.1', client.get_matching_agent_version())
        self.assertEqual('1.23', client.get_matching_agent_version(
                         no_build=True))
        client = client.clone(version='1.20-beta1-series-arch')
        self.assertEqual('1.20-beta1.1', client.get_matching_agent_version())

    def test_upgrade_juju_nonlocal(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'nonlocal'}), '2.0-betaX', None)
        with patch.object(client, '_upgrade_juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(('--agent-version', '2.0'))

    def test_upgrade_juju_local(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '2.0-betaX', None)
        with patch.object(client, '_upgrade_juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(('--agent-version', '2.0',))

    def test_upgrade_juju_no_force_version(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '2.0-betaX', None)
        with patch.object(client, '_upgrade_juju') as juju_mock:
            client.upgrade_juju(force_version=False)
        juju_mock.assert_called_with(())

    def test_clone_unchanged(self):
        client1 = EnvJujuClient(JujuData('foo'), '1.27', 'full/path',
                                debug=True)
        client2 = client1.clone()
        self.assertIsNot(client1, client2)
        self.assertIs(type(client1), type(client2))
        self.assertIs(client1.env, client2.env)
        self.assertEqual(client1.version, client2.version)
        self.assertEqual(client1.full_path, client2.full_path)
        self.assertIs(client1.debug, client2.debug)
        self.assertEqual(client1.feature_flags, client2.feature_flags)
        self.assertEqual(client1._backend, client2._backend)

    def test_clone_changed(self):
        client1 = EnvJujuClient(JujuData('foo'), '1.27', 'full/path',
                                debug=True)
        env2 = SimpleEnvironment('bar')
        client2 = client1.clone(env2, '1.28', 'other/path', debug=False,
                                cls=EnvJujuClient1X)
        self.assertIs(EnvJujuClient1X, type(client2))
        self.assertIs(env2, client2.env)
        self.assertEqual('1.28', client2.version)
        self.assertEqual('other/path', client2.full_path)
        self.assertIs(False, client2.debug)
        self.assertEqual(client1.feature_flags, client2.feature_flags)

    def test_get_cache_path(self):
        client = EnvJujuClient(JujuData('foo', juju_home='/foo/'),
                               '1.27', 'full/path', debug=True)
        self.assertEqual('/foo/models/cache.yaml',
                         client.get_cache_path())

    def test_full_args(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual(('bin', '--show-log', 'bar', '-m', 'foo:foo', 'baz',
                          'qux'), full)
        full = client._full_args('bar', True, ('baz', 'qux'))
        self.assertEqual((
            'bin', '--show-log', 'bar', '-m', 'foo:foo', 'baz', 'qux'), full)
        full = client._full_args('bar', True, ('baz', 'qux'), controller=True)
        self.assertEqual(
            ('bin', '--show-log', 'bar', '-m', 'foo:controller', 'baz', 'qux'),
            full)
        client.env = None
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual(('bin', '--show-log', 'bar', 'baz', 'qux'), full)

    def test_full_args_debug(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin', debug=True)
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual((
            'bin', '--debug', 'bar', '-m', 'foo:foo', 'baz', 'qux'), full)

    def test_full_args_action(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        full = client._full_args('action bar', False, ('baz', 'qux'))
        self.assertEqual(
            ('bin', '--show-log', 'action', 'bar', '-m', 'foo:foo',
             'baz', 'qux'),
            full)

    def test_full_args_controller(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        with patch.object(client, 'get_controller_model_name',
                          return_value='controller') as gamn_mock:
            full = client._full_args('bar', False, ('baz', 'qux'),
                                     controller=True)
        self.assertEqual((
            'bin', '--show-log', 'bar', '-m', 'foo:controller', 'baz', 'qux'),
            full)
        gamn_mock.assert_called_once_with()

    def test_make_model_config_prefers_agent_metadata_url(self):
        env = JujuData('qux', {
            'agent-metadata-url': 'foo',
            'tools-metadata-url': 'bar',
            'type': 'baz',
            })
        client = EnvJujuClient(env, None, 'my/juju/bin')
        self.assertEqual({
            'agent-metadata-url': 'foo',
            'test-mode': True,
            }, client.make_model_config())

    def test__bootstrap_config(self):
        env = JujuData('foo', {
            'access-key': 'foo',
            'admin-secret': 'foo',
            'agent-stream': 'foo',
            'application-id': 'foo',
            'application-password': 'foo',
            'auth-url': 'foo',
            'authorized-keys': 'foo',
            'availability-sets-enabled': 'foo',
            'bootstrap-host': 'foo',
            'bootstrap-timeout': 'foo',
            'bootstrap-user': 'foo',
            'client-email': 'foo',
            'client-id': 'foo',
            'container': 'foo',
            'control-bucket': 'foo',
            'default-series': 'foo',
            'development': False,
            'enable-os-upgrade': 'foo',
            'image-metadata-url': 'foo',
            'location': 'foo',
            'maas-oauth': 'foo',
            'maas-server': 'foo',
            'manta-key-id': 'foo',
            'manta-user': 'foo',
            'management-subscription-id': 'foo',
            'management-certificate': 'foo',
            'name': 'foo',
            'password': 'foo',
            'prefer-ipv6': 'foo',
            'private-key': 'foo',
            'region': 'foo',
            'sdc-key-id': 'foo',
            'sdc-url': 'foo',
            'sdc-user': 'foo',
            'secret-key': 'foo',
            'storage-account-name': 'foo',
            'subscription-id': 'foo',
            'tenant-id': 'foo',
            'tenant-name': 'foo',
            'test-mode': False,
            'tools-metadata-url': 'steve',
            'type': 'foo',
            'username': 'foo',
            }, 'home')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        with client._bootstrap_config() as config_filename:
            with open(config_filename) as f:
                self.assertEqual({
                    'agent-metadata-url': 'steve',
                    'agent-stream': 'foo',
                    'authorized-keys': 'foo',
                    'availability-sets-enabled': 'foo',
                    'bootstrap-timeout': 'foo',
                    'bootstrap-user': 'foo',
                    'container': 'foo',
                    'default-series': 'foo',
                    'development': False,
                    'enable-os-upgrade': 'foo',
                    'image-metadata-url': 'foo',
                    'prefer-ipv6': 'foo',
                    'test-mode': True,
                    }, yaml.safe_load(f))

    def test_get_cloud_region(self):
        self.assertEqual(
            'foo/bar', EnvJujuClient.get_cloud_region('foo', 'bar'))
        self.assertEqual(
            'foo', EnvJujuClient.get_cloud_region('foo', None))

    def test_bootstrap_maas(self):
        env = JujuData('maas', {'type': 'foo', 'region': 'asdf'})
        with patch.object(EnvJujuClient, 'juju') as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            with patch.object(client.env, 'maas', lambda: True):
                with observable_temp_file() as config_file:
                    client.bootstrap()
            mock.assert_called_with(
                'bootstrap', (
                    '--constraints', 'mem=2G spaces=^endpoint-bindings-data,'
                    '^endpoint-bindings-public',
                    'foo/asdf', 'maas',
                    '--config', config_file.name, '--default-model', 'maas',
                    '--agent-version', '2.0'),
                include_e=False)

    def test_bootstrap_maas_spaceless(self):
        # Disable space constraint with environment variable
        os.environ['JUJU_CI_SPACELESSNESS'] = "1"
        env = JujuData('maas', {'type': 'foo', 'region': 'asdf'})
        with patch.object(EnvJujuClient, 'juju') as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            with patch.object(client.env, 'maas', lambda: True):
                with observable_temp_file() as config_file:
                    client.bootstrap()
            mock.assert_called_with(
                'bootstrap', (
                    '--constraints', 'mem=2G',
                    'foo/asdf', 'maas',
                    '--config', config_file.name, '--default-model', 'maas',
                    '--agent-version', '2.0'),
                include_e=False)

    def test_bootstrap_joyent(self):
        env = JujuData('joyent', {
            'type': 'joyent', 'sdc-url': 'https://foo.api.joyentcloud.com'})
        with patch.object(EnvJujuClient, 'juju', autospec=True) as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            with patch.object(client.env, 'joyent', lambda: True):
                with observable_temp_file() as config_file:
                    client.bootstrap()
            mock.assert_called_once_with(
                client, 'bootstrap', (
                    '--constraints', 'mem=2G cpu-cores=1',
                    'joyent/foo', 'joyent',
                    '--config', config_file.name,
                    '--default-model', 'joyent', '--agent-version', '2.0',
                    ), include_e=False)

    def test_bootstrap(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        with observable_temp_file() as config_file:
            with patch.object(EnvJujuClient, 'juju') as mock:
                client = EnvJujuClient(env, '2.0-zeta1', None)
                client.bootstrap()
                mock.assert_called_with(
                    'bootstrap', ('--constraints', 'mem=2G',
                                  'bar/baz', 'foo',
                                  '--config', config_file.name,
                                  '--default-model', 'foo',
                                  '--agent-version', '2.0'), include_e=False)
                config_file.seek(0)
                config = yaml.safe_load(config_file)
        self.assertEqual({'test-mode': True}, config)

    def test_bootstrap_upload_tools(self):
        env = JujuData('foo', {'type': 'foo', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with observable_temp_file() as config_file:
                with patch.object(client, 'juju') as mock:
                    client.bootstrap(upload_tools=True)
            mock.assert_called_with(
                'bootstrap', (
                    '--upload-tools', '--constraints', 'mem=2G',
                    'foo/baz', 'foo',
                    '--config', config_file.name,
                    '--default-model', 'foo'), include_e=False)

    def test_bootstrap_credential(self):
        env = JujuData('foo', {'type': 'foo', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with observable_temp_file() as config_file:
            with patch.object(client, 'juju') as mock:
                client.bootstrap(credential='credential_name')
        mock.assert_called_with(
            'bootstrap', (
                '--constraints', 'mem=2G',
                'foo/baz', 'foo',
                '--config', config_file.name,
                '--default-model', 'foo', '--agent-version', '2.0',
                '--credential', 'credential_name'), include_e=False)

    def test_bootstrap_bootstrap_series(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with patch.object(client, 'juju') as mock:
            with observable_temp_file() as config_file:
                client.bootstrap(bootstrap_series='angsty')
        mock.assert_called_with(
            'bootstrap', (
                '--constraints', 'mem=2G',
                'bar/baz', 'foo',
                '--config', config_file.name, '--default-model', 'foo',
                '--agent-version', '2.0',
                '--bootstrap-series', 'angsty'), include_e=False)

    def test_bootstrap_auto_upgrade(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with patch.object(client, 'juju') as mock:
            with observable_temp_file() as config_file:
                client.bootstrap(auto_upgrade=True)
        mock.assert_called_with(
            'bootstrap', (
                '--constraints', 'mem=2G',
                'bar/baz', 'foo',
                '--config', config_file.name, '--default-model', 'foo',
                '--agent-version', '2.0', '--auto-upgrade'), include_e=False)

    def test_bootstrap_no_gui(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with patch.object(client, 'juju') as mock:
            with observable_temp_file() as config_file:
                client.bootstrap(no_gui=True)
        mock.assert_called_with(
            'bootstrap', (
                '--constraints', 'mem=2G',
                'bar/baz', 'foo',
                '--config', config_file.name, '--default-model', 'foo',
                '--agent-version', '2.0', '--no-gui'), include_e=False)

    def test_bootstrap_metadata(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with patch.object(client, 'juju') as mock:
            with observable_temp_file() as config_file:
                client.bootstrap(metadata_source='/var/test-source')
        mock.assert_called_with(
            'bootstrap', (
                '--constraints', 'mem=2G',
                'bar/baz', 'foo',
                '--config', config_file.name, '--default-model', 'foo',
                '--agent-version', '2.0',
                '--metadata-source', '/var/test-source'), include_e=False)

    def test_bootstrap_to(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with patch.object(client, 'juju') as mock:
            with observable_temp_file() as config_file:
                client.bootstrap(to='target')
        mock.assert_called_with(
            'bootstrap', (
                '--constraints', 'mem=2G',
                'bar/baz', 'foo',
                '--config', config_file.name, '--default-model', 'foo',
                '--agent-version', '2.0', '--to', 'target'), include_e=False)

    def test_bootstrap_async(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        with patch.object(EnvJujuClient, 'juju_async', autospec=True) as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            client.env.juju_home = 'foo'
            with observable_temp_file() as config_file:
                with client.bootstrap_async():
                    mock.assert_called_once_with(
                        client, 'bootstrap', (
                            '--constraints', 'mem=2G',
                            'bar/baz', 'foo',
                            '--config', config_file.name,
                            '--default-model', 'foo',
                            '--agent-version', '2.0'), include_e=False)

    def test_bootstrap_async_upload_tools(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        with patch.object(EnvJujuClient, 'juju_async', autospec=True) as mock:
            client = EnvJujuClient(env, '2.0-zeta1', None)
            with observable_temp_file() as config_file:
                with client.bootstrap_async(upload_tools=True):
                    mock.assert_called_with(
                        client, 'bootstrap', (
                            '--upload-tools', '--constraints', 'mem=2G',
                            'bar/baz', 'foo',
                            '--config', config_file.name,
                            '--default-model', 'foo',
                            ),
                        include_e=False)

    def test_get_bootstrap_args_bootstrap_series(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        args = client.get_bootstrap_args(upload_tools=True,
                                         config_filename='config',
                                         bootstrap_series='angsty')
        self.assertEqual(args, (
            '--upload-tools', '--constraints', 'mem=2G',
            'bar/baz', 'foo',
            '--config', 'config', '--default-model', 'foo',
            '--bootstrap-series', 'angsty'))

    def test_get_bootstrap_args_agent_version(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        args = client.get_bootstrap_args(upload_tools=False,
                                         config_filename='config',
                                         agent_version='2.0-lambda1')
        self.assertEqual(('--constraints', 'mem=2G',
                          'bar/baz', 'foo',
                          '--config', 'config', '--default-model', 'foo',
                          '--agent-version', '2.0-lambda1'), args)

    def test_get_bootstrap_args_upload_tools_and_agent_version(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        client = EnvJujuClient(env, '2.0-zeta1', None)
        with self.assertRaises(ValueError):
            client.get_bootstrap_args(upload_tools=True,
                                      config_filename='config',
                                      agent_version='2.0-lambda1')

    def test_add_model_hypenated_controller(self):
        self.do_add_model(
            'kill-controller', 'add-model', ('-c', 'foo'))

    def do_add_model(self, jes_command, create_cmd, controller_option):
        controller_client = EnvJujuClient(JujuData('foo'), None, None)
        model_data = JujuData('bar', {'type': 'foo'})
        client = EnvJujuClient(model_data, None, None)
        with patch.object(client, 'get_jes_command',
                          return_value=jes_command):
                with patch.object(controller_client, 'juju') as ccj_mock:
                    with observable_temp_file() as config_file:
                        controller_client.add_model(model_data)
        ccj_mock.assert_called_once_with(
            create_cmd, controller_option + (
                'bar', '--config', config_file.name), include_e=False)

    def test_destroy_environment(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        self.assertIs(False, hasattr(client, 'destroy_environment'))

    def test_destroy_model(self):
        env = JujuData('foo', {'type': 'gce'})
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.destroy_model()
        mock.assert_called_with(
            'destroy-model', ('foo', '-y'),
            include_e=False, timeout=600)

    def test_destroy_model_azure(self):
        env = JujuData('foo', {'type': 'azure'})
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.destroy_model()
        mock.assert_called_with(
            'destroy-model', ('foo', '-y'),
            include_e=False, timeout=1800)

    def test_kill_controller(self):
        self.do_kill_controller('kill-controller', 'kill-controller')

    def do_kill_controller(self, jes_command, kill_command):
        client = EnvJujuClient(JujuData('foo', {'type': 'gce'}), None, None)
        with patch.object(client, 'get_jes_command',
                          return_value=jes_command):
            with patch.object(client, 'juju') as juju_mock:
                client.kill_controller()
        juju_mock.assert_called_once_with(
            kill_command, ('foo', '-y'), check=False, include_e=False,
            timeout=600)

    def do_kill_controller_azure(self, jes_command, kill_command):
        client = EnvJujuClient(JujuData('foo', {'type': 'azure'}), None, None)
        with patch.object(client, 'get_jes_command',
                          return_value=jes_command):
            with patch.object(client, 'juju') as juju_mock:
                client.kill_controller()
        juju_mock.assert_called_once_with(
            kill_command, ('foo', '-y'), check=False, include_e=False,
            timeout=1800)

    def test_get_juju_output(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'juju')
        fake_popen = FakePopen('asdf', None, 0)
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_juju_output('bar')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-m', 'foo:foo'),),
                         mock.call_args[0])

    def test_get_juju_output_accepts_varargs(self):
        env = JujuData('foo')
        fake_popen = FakePopen('asdf', None, 0)
        client = EnvJujuClient(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_juju_output('bar', 'baz', '--qux')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-m', 'foo:foo', 'baz',
                           '--qux'),), mock.call_args[0])

    def test_get_juju_output_stderr(self):
        env = JujuData('foo')
        fake_popen = FakePopen(None, 'Hello!', 1)
        client = EnvJujuClient(env, None, 'juju')
        with self.assertRaises(subprocess.CalledProcessError) as exc:
            with patch('subprocess.Popen', return_value=fake_popen):
                client.get_juju_output('bar')
        self.assertEqual(exc.exception.stderr, 'Hello!')

    def test_get_juju_output_merge_stderr(self):
        env = JujuData('foo')
        fake_popen = FakePopen('Err on out', None, 0)
        client = EnvJujuClient(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as mock_popen:
            result = client.get_juju_output('bar', merge_stderr=True)
        self.assertEqual(result, 'Err on out')
        mock_popen.assert_called_once_with(
            ('juju', '--show-log', 'bar', '-m', 'foo:foo'),
            stdin=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE)

    def test_get_juju_output_full_cmd(self):
        env = JujuData('foo')
        fake_popen = FakePopen(None, 'Hello!', 1)
        client = EnvJujuClient(env, None, 'juju')
        with self.assertRaises(subprocess.CalledProcessError) as exc:
            with patch('subprocess.Popen', return_value=fake_popen):
                client.get_juju_output('bar', '--baz', 'qux')
        self.assertEqual(
            ('juju', '--show-log', 'bar', '-m', 'foo:foo', '--baz', 'qux'),
            exc.exception.cmd)

    def test_get_juju_output_accepts_timeout(self):
        env = JujuData('foo')
        fake_popen = FakePopen('asdf', None, 0)
        client = EnvJujuClient(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as po_mock:
            client.get_juju_output('bar', timeout=5)
        self.assertEqual(
            po_mock.call_args[0][0],
            (sys.executable, get_timeout_path(), '5.00', '--', 'juju',
             '--show-log', 'bar', '-m', 'foo:foo'))

    def test__shell_environ_juju_data(self):
        client = EnvJujuClient(
            JujuData('baz', {'type': 'ec2'}), '1.25-foobar', 'path', 'asdf')
        env = client._shell_environ()
        self.assertEqual(env['JUJU_DATA'], 'asdf')
        self.assertNotIn('JUJU_HOME', env)

    def test_juju_output_supplies_path(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, '/foobar/bar')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
            return FakePopen(None, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   side_effect=check_path):
            client.get_juju_output('cmd', 'baz')

    def test_get_status(self):
        output_text = dedent("""\
                - a
                - b
                - c
                """)
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=output_text) as gjo_mock:
            result = client.get_status()
        gjo_mock.assert_called_once_with(
            'show-status', '--format', 'yaml', controller=False)
        self.assertEqual(Status, type(result))
        self.assertEqual(['a', 'b', 'c'], result.status)

    def test_get_status_retries_on_error(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        client.attempt = 0

        def get_juju_output(command, *args, **kwargs):
            if client.attempt == 1:
                return '"hello"'
            client.attempt += 1
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output', get_juju_output):
            client.get_status()

    def test_get_status_raises_on_timeout_1(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)

        def get_juju_output(command, *args, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output',
                          side_effect=get_juju_output):
            with patch('jujupy.until_timeout', lambda x: iter([None, None])):
                with self.assertRaisesRegexp(
                        Exception, 'Timed out waiting for juju status'):
                    client.get_status()

    def test_get_status_raises_on_timeout_2(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch('jujupy.until_timeout', return_value=iter([1])) as mock_ut:
            with patch.object(client, 'get_juju_output',
                              side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    client.get_status(500)
        mock_ut.assert_called_with(500)

    @staticmethod
    def make_status_yaml(key, machine_value, unit_value):
        return dedent("""\
            machines:
              "0":
                {0}: {1}
            applications:
              jenkins:
                units:
                  jenkins/0:
                    {0}: {2}
        """.format(key, machine_value, unit_value))

    def test_deploy_non_joyent(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_joyent(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_repository(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('/home/jrandom/repo/mongodb')
        mock_juju.assert_called_with(
            'deploy', ('/home/jrandom/repo/mongodb',))

    def test_deploy_to(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', to='0')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--to', '0'))

    def test_deploy_service(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('local:mondogb', service='my-mondogb')
        mock_juju.assert_called_with(
            'deploy', ('local:mondogb', 'my-mondogb',))

    def test_deploy_force(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('local:mondogb', force=True)
        mock_juju.assert_called_with('deploy', ('local:mondogb', '--force',))

    def test_deploy_series(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('local:blah', series='xenial')
        mock_juju.assert_called_with(
            'deploy', ('local:blah', '--series', 'xenial'))

    def test_deploy_resource(self):
        env = EnvJujuClient(JujuData('foo', {'type': 'local'}), None, None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('local:blah', resource='foo=/path/dir')
        mock_juju.assert_called_with(
            'deploy', ('local:blah', '--resource', 'foo=/path/dir'))

    def test_deploy_storage(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', storage='rootfs,1G')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--storage', 'rootfs,1G'))

    def test_deploy_constraints(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', constraints='virt-type=kvm')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--constraints', 'virt-type=kvm'))

    def test_attach(self):
        env = EnvJujuClient(JujuData('foo', {'type': 'local'}), None, None)
        with patch.object(env, 'juju') as mock_juju:
            env.attach('foo', resource='foo=/path/dir')
        mock_juju.assert_called_with('attach', ('foo', 'foo=/path/dir'))

    def test_list_resources(self):
        data = 'resourceid: resource/foo'
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(
                client, 'get_juju_output', return_value=data) as mock_gjo:
            status = client.list_resources('foo')
        self.assertEqual(status, yaml.safe_load(data))
        mock_gjo.assert_called_with(
            'list-resources', '--format', 'yaml', 'foo', '--details')

    def test_wait_for_resource(self):
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(
                client, 'list_resources',
                return_value=make_resource_list()) as mock_lr:
            client.wait_for_resource('dummy-resource/foo', 'foo')
        mock_lr.assert_called_once_with('foo')

    def test_wait_for_resource_timeout(self):
        client = EnvJujuClient(JujuData('local'), None, None)
        resource_list = make_resource_list()
        resource_list['resources'][0]['expected']['resourceid'] = 'bad_id'
        with patch.object(
                client, 'list_resources',
                return_value=resource_list) as mock_lr:
            with patch('jujupy.until_timeout', autospec=True,
                       return_value=[0, 1]) as mock_ju:
                with patch('time.sleep', autospec=True) as mock_ts:
                    with self.assertRaisesRegexp(
                            JujuResourceTimeout,
                            'Timeout waiting for a resource to be downloaded'):
                        client.wait_for_resource('dummy-resource/foo', 'foo')
        calls = [call('foo'), call('foo')]
        self.assertEqual(mock_lr.mock_calls, calls)
        self.assertEqual(mock_ts.mock_calls, [call(.1), call(.1)])
        self.assertEqual(mock_ju.mock_calls, [call(60)])

    def test_wait_for_resource_suppresses_deadline(self):
        with client_past_deadline() as client:
            real_check_timeouts = client.check_timeouts

            def list_resources(service_or_unit):
                with real_check_timeouts():
                    return make_resource_list()

            with patch.object(client, 'check_timeouts', autospec=True):
                with patch.object(client, 'list_resources', autospec=True,
                                  side_effect=list_resources):
                        client.wait_for_resource('dummy-resource/foo',
                                                 'app_unit')

    def test_wait_for_resource_checks_deadline(self):
        resource_list = make_resource_list()
        with client_past_deadline() as client:
            with patch.object(client, 'list_resources', autospec=True,
                              return_value=resource_list):
                with self.assertRaises(SoftDeadlineExceeded):
                    client.wait_for_resource('dummy-resource/foo', 'app_unit')

    def test_deploy_bundle_2x(self):
        client = EnvJujuClient(JujuData('an_env', None),
                               '1.23-series-arch', None)
        with patch.object(client, 'juju') as mock_juju:
            client.deploy_bundle('bundle:~juju-qa/some-bundle')
        mock_juju.assert_called_with(
            'deploy', ('bundle:~juju-qa/some-bundle'), timeout=3600)

    def test_deploy_bundle_template(self):
        client = EnvJujuClient(JujuData('an_env', None),
                               '1.23-series-arch', None)
        with patch.object(client, 'juju') as mock_juju:
            client.deploy_bundle('bundle:~juju-qa/some-{container}-bundle')
        mock_juju.assert_called_with(
            'deploy', ('bundle:~juju-qa/some-lxd-bundle'), timeout=3600)

    def test_upgrade_charm(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '2.34-74', None)
        with patch.object(env, 'juju') as mock_juju:
            env.upgrade_charm('foo-service',
                              '/bar/repository/angsty/mongodb')
        mock_juju.assert_called_once_with(
            'upgrade-charm', ('foo-service', '--path',
                              '/bar/repository/angsty/mongodb',))

    def test_remove_service(self):
        env = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.remove_service('mondogb')
        mock_juju.assert_called_with('remove-application', ('mondogb',))

    def test_status_until_always_runs_once(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        status_txt = self.make_status_yaml('agent-state', 'started', 'started')
        with patch.object(client, 'get_juju_output', return_value=status_txt):
            result = list(client.status_until(-1))
        self.assertEqual(
            [r.status for r in result], [Status.from_text(status_txt).status])

    def test_status_until_timeout(self):
        client = EnvJujuClient(
            JujuData('foo', {'type': 'local'}), '1.234-76', None)
        status_txt = self.make_status_yaml('agent-state', 'started', 'started')
        status_yaml = yaml.safe_load(status_txt)

        def until_timeout_stub(timeout, start=None):
            return iter([None, None])

        with patch.object(client, 'get_juju_output', return_value=status_txt):
            with patch('jujupy.until_timeout',
                       side_effect=until_timeout_stub) as ut_mock:
                result = list(client.status_until(30, 70))
        self.assertEqual(
            [r.status for r in result], [status_yaml] * 3)
        # until_timeout is called by status as well as status_until.
        self.assertEqual(ut_mock.mock_calls,
                         [call(60), call(30, start=70), call(60), call(60)])

    def test_status_until_suppresses_deadline(self):
        with self.only_status_checks() as client:
            list(client.status_until(0))

    def test_status_until_checks_deadline(self):
        with self.status_does_not_check() as client:
            with self.assertRaises(SoftDeadlineExceeded):
                list(client.status_until(0))

    def test_add_ssh_machines(self):
        client = EnvJujuClient(JujuData('foo'), None, 'juju')
        with patch('subprocess.check_call', autospec=True) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-foo'),
            0)
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-bar'),
            1)
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-baz'),
            2)
        self.assertEqual(cc_mock.call_count, 3)

    def test_add_ssh_machines_retry(self):
        client = EnvJujuClient(JujuData('foo'), None, 'juju')
        with patch('subprocess.check_call', autospec=True,
                   side_effect=[subprocess.CalledProcessError(None, None),
                                None, None, None]) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-foo'),
            0)
        self.pause_mock.assert_called_once_with(30)
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-foo'),
            1)
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-bar'),
            2)
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-baz'),
            3)
        self.assertEqual(cc_mock.call_count, 4)

    def test_add_ssh_machines_fail_on_second_machine(self):
        client = EnvJujuClient(JujuData('foo'), None, 'juju')
        with patch('subprocess.check_call', autospec=True, side_effect=[
                None, subprocess.CalledProcessError(None, None), None, None
                ]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-foo'),
            0)
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-bar'),
            1)
        self.assertEqual(cc_mock.call_count, 2)

    def test_add_ssh_machines_fail_on_second_attempt(self):
        client = EnvJujuClient(JujuData('foo'), None, 'juju')
        with patch('subprocess.check_call', autospec=True, side_effect=[
                subprocess.CalledProcessError(None, None),
                subprocess.CalledProcessError(None, None)]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-foo'),
            0)
        assert_juju_call(
            self,
            cc_mock,
            client,
            ('juju', '--show-log', 'add-machine',
             '-m', 'foo:foo', 'ssh:m-foo'),
            1)
        self.assertEqual(cc_mock.call_count, 2)

    def test_wait_for_started(self):
        value = self.make_status_yaml('agent-state', 'started', 'started')
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_started()

    def test_wait_for_started_timeout(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch('jujupy.until_timeout', lambda x, start=None: range(1)):
            with patch.object(client, 'get_juju_output', return_value=value):
                writes = []
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for agents to start in local'):
                        client.wait_for_started()
                self.assertEqual(writes, ['pending: 0', ' .', '\n'])

    def test_wait_for_started_start(self):
        value = self.make_status_yaml('agent-state', 'started', 'pending')
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                writes = []
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for agents to start in local'):
                        client.wait_for_started(start=now - timedelta(1200))
                self.assertEqual(writes, ['pending: jenkins/0', '\n'])

    def make_ha_status(self):
        return {'machines': {
            '0': {'controller-member-status': 'has-vote'},
            '1': {'controller-member-status': 'has-vote'},
            '2': {'controller-member-status': 'has-vote'},
            }}

    @contextmanager
    def only_status_checks(self, status=None):
        """This context manager ensure only get_status calls check_timeouts.

        Everything else will get a mock object.

        Also, the client is patched so that the soft_deadline has been hit.
        """
        with client_past_deadline() as client:
            # This will work even after we patch check_timeouts below.
            real_check_timeouts = client.check_timeouts

            def check(timeout=60, controller=False):
                with real_check_timeouts():
                    return client.status_class(status, '')

            with patch.object(client, 'get_status', autospec=True,
                              side_effect=check):
                with patch.object(client, 'check_timeouts', autospec=True):
                    yield client

    def test__wait_for_status_suppresses_deadline(self):

        def translate(x):
            return None

        with self.only_status_checks() as client:
            client._wait_for_status(Mock(), translate)

    @contextmanager
    def status_does_not_check(self, status=None):
        """This context manager ensure get_status never calls check_timeouts.

        Also, the client is patched so that the soft_deadline has been hit.
        """
        with client_past_deadline() as client:
            status_obj = client.status_class(status, '')
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status_obj):
                yield client

    def test__wait_for_status_checks_deadline(self):

        def translate(x):
            return None

        with self.status_does_not_check() as client:
            with self.assertRaises(SoftDeadlineExceeded):
                client._wait_for_status(Mock(), translate)

    def test_wait_for_started_logs_status(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            writes = []
            with patch.object(GroupReporter, '_write', autospec=True,
                              side_effect=lambda _, s: writes.append(s)):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_started(0)
            self.assertEqual(writes, ['pending: 0', '\n'])
        self.assertEqual(self.log_stream.getvalue(), 'ERROR %s\n' % value)

    def test_wait_for_subordinate_units(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1/0:
                        agent-state: started
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub2/0:
                        agent-state: started
                      sub3/0:
                        agent-state: started
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'jenkins', 'sub1', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_subordinate_units_with_agent_status(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1/0:
                        agent-status:
                          current: idle
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub2/0:
                        agent-status:
                          current: idle
                      sub3/0:
                        agent-status:
                          current: idle
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'jenkins', 'sub1', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_multiple_subordinate_units(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub/0:
                        agent-state: started
                  ubuntu/1:
                    subordinates:
                      sub/1:
                        agent-state: started
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'ubuntu', 'sub', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_subordinate_units_checks_slash_in_unit_name(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            applications:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1:
                        agent-state: started
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_subordinate_units(
                        'jenkins', 'sub1', start=now - timedelta(1200))

    def test_wait_for_subordinate_units_no_subordinate(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            applications:
              jenkins:
                units:
                  jenkins/0:
                    agent-state: started
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_subordinate_units(
                        'jenkins', 'sub1', start=now - timedelta(1200))

    def test_wait_for_workload(self):
        initial_status = Status.from_text("""\
            applications:
              jenkins:
                units:
                  jenkins/0:
                    workload-status:
                      current: waiting
                  subordinates:
                    ntp/0:
                      workload-status:
                        current: unknown
        """)
        final_status = Status(copy.deepcopy(initial_status.status), None)
        final_status.status['applications']['jenkins']['units']['jenkins/0'][
            'workload-status']['current'] = 'active'
        client = EnvJujuClient(JujuData('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[1]):
            with patch.object(client, 'get_status', autospec=True,
                              side_effect=[initial_status, final_status]):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads()
        self.assertEqual(writes, ['waiting: jenkins/0', '\n'])

    def test_wait_for_workload_all_unknown(self):
        status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    workload-status:
                      current: unknown
                  subordinates:
                    ntp/0:
                      workload-status:
                        current: unknown
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[]):
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads(timeout=1)
        self.assertEqual(writes, [])

    def test_wait_for_workload_no_workload_status(self):
        status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    agent-state: active
        """)
        client = EnvJujuClient(JujuData('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[]):
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads(timeout=1)
        self.assertEqual(writes, [])

    def test_list_models(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'juju') as j_mock:
            client.list_models()
        j_mock.assert_called_once_with(
            'list-models', ('-c', 'foo'), include_e=False)

    def test_get_models(self):
        data = """\
            models:
            - name: foo
              model-uuid: aaaa
              owner: admin
            - name: bar
              model-uuid: bbbb
              owner: admin
            current-model: foo
        """
        client = EnvJujuClient(JujuData('baz'), None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=data) as gjo_mock:
            models = client.get_models()
        gjo_mock.assert_called_once_with(
            'list-models', '-c', 'baz', '--format', 'yaml',
            include_e=False, timeout=120)
        expected_models = {
            'models': [
                {'name': 'foo', 'model-uuid': 'aaaa', 'owner': 'admin'},
                {'name': 'bar', 'model-uuid': 'bbbb', 'owner': 'admin'}],
            'current-model': 'foo'
        }
        self.assertEqual(expected_models, models)

    def test_iter_model_clients(self):
        data = """\
            models:
            - name: foo
              model-uuid: aaaa
              owner: admin
            - name: bar
              model-uuid: bbbb
              owner: admin
            current-model: foo
        """
        client = EnvJujuClient(JujuData('foo', {}), None, None)
        with patch.object(client, 'get_juju_output', return_value=data):
            model_clients = list(client.iter_model_clients())
        self.assertEqual(2, len(model_clients))
        self.assertIs(client, model_clients[0])
        self.assertEqual('bar', model_clients[1].env.environment)

    def test_get_controller_model_name(self):
        models = {
            'models': [
                {'name': 'controller', 'model-uuid': 'aaaa'},
                {'name': 'bar', 'model-uuid': 'bbbb'}],
            'current-model': 'bar'
        }
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_models',
                          return_value=models) as gm_mock:
            controller_name = client.get_controller_model_name()
        self.assertEqual(0, gm_mock.call_count)
        self.assertEqual('controller', controller_name)

    def test_get_controller_model_name_without_controller(self):
        models = {
            'models': [
                {'name': 'bar', 'model-uuid': 'aaaa'},
                {'name': 'baz', 'model-uuid': 'bbbb'}],
            'current-model': 'bar'
        }
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_models', return_value=models):
            controller_name = client.get_controller_model_name()
        self.assertEqual('controller', controller_name)

    def test_get_controller_model_name_no_models(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_models', return_value={}):
            controller_name = client.get_controller_model_name()
        self.assertEqual('controller', controller_name)

    def test_get_model_uuid_returns_uuid(self):
        model_uuid = '9ed1bde9-45c6-4d41-851d-33fdba7fa194'
        yaml_string = dedent("""\
        foo:
          name: foo
          model-uuid: {uuid}
          controller-uuid: eb67e1eb-6c54-45f5-8b6a-b6243be97202
          owner: admin
          cloud: lxd
          region: localhost
          type: lxd
          life: alive
          status:
            current: available
            since: 1 minute ago
          users:
            admin:
              display-name: admin
              access: admin
              last-connection: just now
            """.format(uuid=model_uuid))
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output') as m_get_juju_output:
            m_get_juju_output.return_value = yaml_string
            self.assertEqual(
                client.get_model_uuid(),
                model_uuid
            )
            m_get_juju_output.assert_called_once_with(
                'show-model', '--format', 'yaml', 'foo:foo', include_e=False)

    def test_get_controller_model_uuid_returns_uuid(self):
        controller_uuid = 'eb67e1eb-6c54-45f5-8b6a-b6243be97202'
        controller_model_uuid = '1c908e10-4f07-459a-8419-bb61553a4660'
        yaml_string = dedent("""\
        controller:
          name: controller
          model-uuid: {model}
          controller-uuid: {controller}
          controller-name: localtempveebers
          owner: admin
          cloud: lxd
          region: localhost
          type: lxd
          life: alive
          status:
            current: available
            since: 59 seconds ago
          users:
            admin:
              display-name: admin
              access: admin
              last-connection: just now""".format(model=controller_model_uuid,
                                                  controller=controller_uuid))
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output') as m_get_juju_output:
            m_get_juju_output.return_value = yaml_string
            self.assertEqual(
                client.get_controller_model_uuid(),
                controller_model_uuid
            )
            m_get_juju_output.assert_called_once_with(
                'show-model', 'controller',
                '--format', 'yaml', include_e=False)

    def test_get_controller_uuid_returns_uuid(self):
        controller_uuid = 'eb67e1eb-6c54-45f5-8b6a-b6243be97202'
        yaml_string = dedent("""\
        foo:
          details:
            uuid: {uuid}
            api-endpoints: ['10.194.140.213:17070']
            cloud: lxd
            region: localhost
          models:
            controller:
              uuid: {uuid}
            default:
              uuid: 772cdd39-b454-4bd5-8704-dc9aa9ff1750
          current-model: default
          account:
            user: admin
          bootstrap-config:
            config:
            cloud: lxd
            cloud-type: lxd
            region: localhost""".format(uuid=controller_uuid))
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output') as m_get_juju_output:
            m_get_juju_output.return_value = yaml_string
            self.assertEqual(
                client.get_controller_uuid(),
                controller_uuid
            )
            m_get_juju_output.assert_called_once_with(
                'show-controller', '--format', 'yaml', include_e=False)

    def test_get_controller_client(self):
        client = EnvJujuClient(
            JujuData('foo', {'bar': 'baz'}, 'myhome'), None, None)
        controller_client = client.get_controller_client()
        controller_env = controller_client.env
        self.assertEqual('controller', controller_env.environment)
        self.assertEqual(
            {'bar': 'baz', 'name': 'controller'}, controller_env.config)

    def test_list_controllers(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'juju') as j_mock:
            client.list_controllers()
        j_mock.assert_called_once_with('list-controllers', (), include_e=False)

    def test_get_controller_endpoint_ipv4(self):
        data = """\
          foo:
            details:
              api-endpoints: ['10.0.0.1:17070', '10.0.0.2:17070']
        """
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=data) as gjo_mock:
            endpoint = client.get_controller_endpoint()
        self.assertEqual('10.0.0.1', endpoint)
        gjo_mock.assert_called_once_with(
            'show-controller', 'foo', include_e=False)

    def test_get_controller_endpoint_ipv6(self):
        data = """\
          foo:
            details:
              api-endpoints: ['[::1]:17070', '[fe80::216:3eff:0:9dc7]:17070']
        """
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=data) as gjo_mock:
            endpoint = client.get_controller_endpoint()
        self.assertEqual('::1', endpoint)
        gjo_mock.assert_called_once_with(
            'show-controller', 'foo', include_e=False)

    def test_get_controller_controller_name(self):
        data = """\
          bar:
            details:
              api-endpoints: ['[::1]:17070', '[fe80::216:3eff:0:9dc7]:17070']
        """
        client = EnvJujuClient(JujuData('foo', {}), None, None)
        controller_client = client.get_controller_client()
        client.env.controller.name = 'bar'
        with patch.object(controller_client, 'get_juju_output',
                          return_value=data) as gjo:
            endpoint = controller_client.get_controller_endpoint()
        gjo.assert_called_once_with('show-controller', 'bar',
                                    include_e=False)
        self.assertEqual('::1', endpoint)

    def test_get_controller_members(self):
        status = Status.from_text("""\
            model: controller
            machines:
              "0":
                dns-name: 10.0.0.0
                instance-id: juju-aaaa-machine-0
                controller-member-status: has-vote
              "1":
                dns-name: 10.0.0.1
                instance-id: juju-bbbb-machine-1
              "2":
                dns-name: 10.0.0.2
                instance-id: juju-cccc-machine-2
                controller-member-status: has-vote
              "3":
                dns-name: 10.0.0.3
                instance-id: juju-dddd-machine-3
                controller-member-status: has-vote
        """)
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_status', autospec=True,
                          return_value=status):
            with patch.object(client, 'get_controller_endpoint', autospec=True,
                              return_value='10.0.0.3') as gce_mock:
                with patch.object(client, 'get_controller_member_status',
                                  wraps=client.get_controller_member_status,
                                  ) as gcms_mock:
                    members = client.get_controller_members()
        # Machine 1 was ignored. Machine 3 is the leader, thus first.
        expected = [
            Machine('3', {
                'dns-name': '10.0.0.3',
                'instance-id': 'juju-dddd-machine-3',
                'controller-member-status': 'has-vote'}),
            Machine('0', {
                'dns-name': '10.0.0.0',
                'instance-id': 'juju-aaaa-machine-0',
                'controller-member-status': 'has-vote'}),
            Machine('2', {
                'dns-name': '10.0.0.2',
                'instance-id': 'juju-cccc-machine-2',
                'controller-member-status': 'has-vote'}),
        ]
        self.assertEqual(expected, members)
        gce_mock.assert_called_once_with()
        # get_controller_member_status must be called to ensure compatibility
        # with all version of Juju.
        self.assertEqual(4, gcms_mock.call_count)

    def test_get_controller_members_one(self):
        status = Status.from_text("""\
            model: controller
            machines:
              "0":
                dns-name: 10.0.0.0
                instance-id: juju-aaaa-machine-0
                controller-member-status: has-vote
        """)
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_status', autospec=True,
                          return_value=status):
            with patch.object(client, 'get_controller_endpoint') as gce_mock:
                members = client.get_controller_members()
        # Machine 0 was the only choice, no need to find the leader.
        expected = [
            Machine('0', {
                'dns-name': '10.0.0.0',
                'instance-id': 'juju-aaaa-machine-0',
                'controller-member-status': 'has-vote'}),
        ]
        self.assertEqual(expected, members)
        self.assertEqual(0, gce_mock.call_count)

    def test_get_controller_leader(self):
        members = [
            Machine('3', {}),
            Machine('0', {}),
            Machine('2', {}),
        ]
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_controller_members', autospec=True,
                          return_value=members):
            leader = client.get_controller_leader()
        self.assertEqual(Machine('3', {}), leader)

    def test_wait_for_ha(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'controller-member-status': 'has-vote'},
                '1': {'controller-member-status': 'has-vote'},
                '2': {'controller-member-status': 'has-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=value) as gjo_mock:
            client.wait_for_ha()
        gjo_mock.assert_called_once_with(
            'show-status', '--format', 'yaml', controller=True)

    def test_wait_for_ha_no_has_vote(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'controller-member-status': 'no-vote'},
                '1': {'controller-member-status': 'no-vote'},
                '2': {'controller-member-status': 'no-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            writes = []
            with patch('jujupy.until_timeout', autospec=True,
                       return_value=[2, 1]):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for voting to be enabled.'):
                        client.wait_for_ha()
            self.assertEqual(writes[:2], ['no-vote: 0, 1, 2', ' .'])
            self.assertEqual(writes[2:-1], ['.'] * (len(writes) - 3))
            self.assertEqual(writes[-1:], ['\n'])

    def test_wait_for_ha_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'controller-member-status': 'has-vote'},
                '1': {'controller-member-status': 'has-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for voting to be enabled.'):
                    client.wait_for_ha()

    def test_wait_for_ha_timeout_with_status_error(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state-info': 'running'},
                '1': {'agent-state-info': 'error: foo'},
            },
            'services': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch('jujupy.until_timeout', autospec=True, return_value=[2, 1]):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        ErroredUnit, '1 is in state error: foo'):
                    client.wait_for_ha()

    def test_wait_for_ha_suppresses_deadline(self):
        with self.only_status_checks(self.make_ha_status()) as client:
            client.wait_for_ha()

    def test_wait_for_ha_checks_deadline(self):
        with self.status_does_not_check(self.make_ha_status()) as client:
            with self.assertRaises(SoftDeadlineExceeded):
                client.wait_for_ha()

    def test_wait_for_deploy_started(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'baz': 'qux'}
                    }
                }
            }
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_deploy_started()

    def test_wait_for_deploy_started_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'applications': {},
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for services to start.'):
                    client.wait_for_deploy_started()

    def make_deployed_status(self):
        return {
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'baz': 'qux'}
                    }
                }
            }
        }

    def test_wait_for_deploy_started_suppresses_deadline(self):
        with self.only_status_checks(self.make_deployed_status()) as client:
            client.wait_for_deploy_started()

    def test_wait_for_deploy_started_checks_deadline(self):
        with self.status_does_not_check(self.make_deployed_status()) as client:
            with self.assertRaises(SoftDeadlineExceeded):
                client.wait_for_deploy_started()

    def test_wait_for_version(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_timeout(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.1')
        client = EnvJujuClient(JujuData('local'), None, None)
        writes = []
        with patch('jujupy.until_timeout', lambda x, start=None: [x]):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception, 'Some versions did not update'):
                        client.wait_for_version('1.17.2')
        self.assertEqual(writes, ['1.17.1: jenkins/0', ' .', '\n'])

    def test_wait_for_version_handles_connection_error(self):
        err = subprocess.CalledProcessError(2, 'foo')
        err.stderr = 'Unable to connect to environment'
        err = CannotConnectEnv(err)
        status = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        actions = [err, status]

        def get_juju_output_fake(*args, **kwargs):
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            else:
                return action

        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', get_juju_output_fake):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_raises_non_connection_error(self):
        err = Exception('foo')
        status = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        actions = [err, status]

        def get_juju_output_fake(*args, **kwargs):
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            else:
                return action

        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', get_juju_output_fake):
            with self.assertRaisesRegexp(Exception, 'foo'):
                client.wait_for_version('1.17.2')

    def test_wait_for_just_machine_0(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for('machines-not-0', 'none')

    def test_wait_for_just_machine_0_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
                '1': {'agent-state': 'started'},
            },
        })
        client = EnvJujuClient(JujuData('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value), \
            patch('jujupy.until_timeout', lambda x: range(0)), \
            self.assertRaisesRegexp(
                Exception,
                'Timed out waiting for machines-not-0'):
            client.wait_for('machines-not-0', 'none')

    def test_set_model_constraints(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.set_model_constraints({'bar': 'baz'})
        juju_mock.assert_called_once_with('set-model-constraints',
                                          ('bar=baz',))

    def test_get_model_config(self):
        env = JujuData('foo', None)
        fake_popen = FakePopen(yaml.safe_dump({'bar': 'baz'}), None, 0)
        client = EnvJujuClient(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as po_mock:
            result = client.get_model_config()
        assert_juju_call(
            self, po_mock, client, (
                'juju', '--show-log',
                'model-config', '-m', 'foo:foo', '--format', 'yaml'))
        self.assertEqual({'bar': 'baz'}, result)

    def test_get_env_option(self):
        env = JujuData('foo', None)
        fake_popen = FakePopen('https://example.org/juju/tools', None, 0)
        client = EnvJujuClient(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_env_option('tools-metadata-url')
        self.assertEqual(
            mock.call_args[0][0],
            ('juju', '--show-log', 'model-config', '-m', 'foo:foo',
             'tools-metadata-url'))
        self.assertEqual('https://example.org/juju/tools', result)

    def test_set_env_option(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'juju')
        with patch('subprocess.check_call') as mock:
            client.set_env_option(
                'tools-metadata-url', 'https://example.org/juju/tools')
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(
            ('juju', '--show-log', 'model-config', '-m', 'foo:foo',
             'tools-metadata-url=https://example.org/juju/tools'))

    def test_unset_env_option(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'juju')
        with patch('subprocess.check_call') as mock:
            client.unset_env_option('tools-metadata-url')
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(
            ('juju', '--show-log', 'model-config', '-m', 'foo:foo',
             '--reset', 'tools-metadata-url'))

    def test_set_testing_agent_metadata_url(self):
        env = JujuData(None, {'type': 'foo'})
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                client.set_testing_agent_metadata_url()
        mock_get.assert_called_with('agent-metadata-url')
        mock_set.assert_called_with(
            'agent-metadata-url',
            'https://example.org/juju/testing/tools')

    def test_set_testing_agent_metadata_url_noop(self):
        env = JujuData(None, {'type': 'foo'})
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/testing/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                client.set_testing_agent_metadata_url()
        mock_get.assert_called_with('agent-metadata-url',)
        self.assertEqual(0, mock_set.call_count)

    def test_juju(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, 'juju')
        with patch('subprocess.check_call') as mock:
            client.juju('foo', ('bar', 'baz'))
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(('juju', '--show-log', 'foo', '-m', 'qux:qux',
                                 'bar', 'baz'))

    def test_expect_returns_pexpect_spawn_object(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, 'juju')
        with patch('pexpect.spawn') as mock:
            process = client.expect('foo', ('bar', 'baz'))

        self.assertIs(process, mock.return_value)
        mock.assert_called_once_with('juju --show-log foo -m qux:qux bar baz')

    def test_expect_uses_provided_envvar_path(self):
        from pexpect import ExceptionPexpect
        env = JujuData('qux')
        client = EnvJujuClient(env, None, 'juju')

        with temp_dir() as empty_path:
            broken_envvars = dict(PATH=empty_path)
            self.assertRaises(
                ExceptionPexpect,
                client.expect,
                'ls', (), extra_env=broken_envvars,
                )

    def test_juju_env(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
        with patch('subprocess.check_call', side_effect=check_path):
            client.juju('foo', ('bar', 'baz'))

    def test_juju_no_check(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, 'juju')
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        with patch('subprocess.call') as mock:
            client.juju('foo', ('bar', 'baz'), check=False)
        mock.assert_called_with(('juju', '--show-log', 'foo', '-m', 'qux:qux',
                                 'bar', 'baz'))

    def test_juju_no_check_env(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
        with patch('subprocess.call', side_effect=check_path):
            client.juju('foo', ('bar', 'baz'), check=False)

    def test_juju_timeout(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.check_call') as cc_mock:
            client.juju('foo', ('bar', 'baz'), timeout=58)
        self.assertEqual(cc_mock.call_args[0][0], (
            sys.executable, get_timeout_path(), '58.00', '--', 'baz',
            '--show-log', 'foo', '-m', 'qux:qux', 'bar', 'baz'))

    def test_juju_juju_home(self):
        env = JujuData('qux')
        os.environ['JUJU_HOME'] = 'foo'
        client = EnvJujuClient(env, None, '/foobar/baz')

        def check_home(*args, **kwargs):
            self.assertEqual(os.environ['JUJU_HOME'], 'foo')
            yield
            self.assertEqual(os.environ['JUJU_HOME'], 'asdf')
            yield

        with patch('subprocess.check_call', side_effect=check_home):
            client.juju('foo', ('bar', 'baz'))
            client.env.juju_home = 'asdf'
            client.juju('foo', ('bar', 'baz'))

    def test_juju_extra_env(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, 'juju')
        extra_env = {'JUJU': '/juju', 'JUJU_HOME': client.env.juju_home}

        def check_env(*args, **kwargs):
            self.assertEqual('/juju', os.environ['JUJU'])

        with patch('subprocess.check_call', side_effect=check_env) as mock:
            client.juju('quickstart', ('bar', 'baz'), extra_env=extra_env)
        mock.assert_called_with(
            ('juju', '--show-log', 'quickstart', '-m', 'qux:qux',
             'bar', 'baz'))

    def test_juju_backup_with_tgz(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')

        with patch(
                'subprocess.Popen',
                return_value=FakePopen('foojuju-backup-24.tgzz', '', 0),
                ) as popen_mock:
            backup_file = client.backup()
        self.assertEqual(backup_file, os.path.abspath('juju-backup-24.tgz'))
        assert_juju_call(self, popen_mock, client, ('baz', '--show-log',
                         'create-backup', '-m', 'qux:qux'))

    def test_juju_backup_with_tar_gz(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.Popen',
                   return_value=FakePopen(
                       'foojuju-backup-123-456.tar.gzbar', '', 0)):
            backup_file = client.backup()
        self.assertEqual(
            backup_file, os.path.abspath('juju-backup-123-456.tar.gz'))

    def test_juju_backup_no_file(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.Popen', return_value=FakePopen('', '', 0)):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                client.backup()

    def test_juju_backup_wrong_file(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.Popen',
                   return_value=FakePopen('mumu-backup-24.tgz', '', 0)):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                client.backup()

    def test_juju_backup_environ(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        environ = client._shell_environ()

        def side_effect(*args, **kwargs):
            self.assertEqual(environ, os.environ)
            return FakePopen('foojuju-backup-123-456.tar.gzbar', '', 0)
        with patch('subprocess.Popen', side_effect=side_effect):
            client.backup()
            self.assertNotEqual(environ, os.environ)

    def test_restore_backup(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch.object(client, 'juju') as gjo_mock:
            client.restore_backup('quxx')
        gjo_mock.assert_called_once_with(
            'restore-backup',
            ('-b', '--constraints', 'mem=2G', '--file', 'quxx'))

    def test_restore_backup_async(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch.object(client, 'juju_async') as gjo_mock:
            result = client.restore_backup_async('quxx')
        gjo_mock.assert_called_once_with('restore-backup', (
            '-b', '--constraints', 'mem=2G', '--file', 'quxx'))
        self.assertIs(gjo_mock.return_value, result)

    def test_enable_ha(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch.object(client, 'juju', autospec=True) as eha_mock:
            client.enable_ha()
        eha_mock.assert_called_once_with(
            'enable-ha', ('-n', '3', '-c', 'qux'), include_e=False)

    def test_juju_async(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.Popen') as popen_class_mock:
            with client.juju_async('foo', ('bar', 'baz')) as proc:
                assert_juju_call(
                    self,
                    popen_class_mock,
                    client,
                    ('baz', '--show-log', 'foo', '-m', 'qux:qux',
                     'bar', 'baz'))
                self.assertIs(proc, popen_class_mock.return_value)
                self.assertEqual(proc.wait.call_count, 0)
                proc.wait.return_value = 0
        proc.wait.assert_called_once_with()

    def test_juju_async_failure(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        with patch('subprocess.Popen') as popen_class_mock:
            with self.assertRaises(subprocess.CalledProcessError) as err_cxt:
                with client.juju_async('foo', ('bar', 'baz')):
                    proc_mock = popen_class_mock.return_value
                    proc_mock.wait.return_value = 23
        self.assertEqual(err_cxt.exception.returncode, 23)
        self.assertEqual(err_cxt.exception.cmd, (
            'baz', '--show-log', 'foo', '-m', 'qux:qux', 'bar', 'baz'))

    def test_juju_async_environ(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        environ = client._shell_environ()
        proc_mock = Mock()
        with patch('subprocess.Popen') as popen_class_mock:

            def check_environ(*args, **kwargs):
                self.assertEqual(environ, os.environ)
                return proc_mock
            popen_class_mock.side_effect = check_environ
            proc_mock.wait.return_value = 0
            with client.juju_async('foo', ('bar', 'baz')):
                pass
            self.assertNotEqual(environ, os.environ)

    def test_is_jes_enabled(self):
        # EnvJujuClient knows that JES is always enabled, and doesn't need to
        # shell out.
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        fake_popen = FakePopen(' %s' % SYSTEM, None, 0)
        with patch('subprocess.Popen',
                   return_value=fake_popen) as po_mock:
            self.assertTrue(client.is_jes_enabled())
        self.assertEqual(0, po_mock.call_count)

    def test_get_jes_command(self):
        env = JujuData('qux')
        client = EnvJujuClient(env, None, '/foobar/baz')
        # Juju 1.24 and older do not have a JES command. It is an error
        # to call get_jes_command when is_jes_enabled is False
        fake_popen = FakePopen(' %s' % SYSTEM, None, 0)
        with patch('subprocess.Popen',
                   return_value=fake_popen) as po_mock:
            self.assertEqual(KILL_CONTROLLER, client.get_jes_command())
        self.assertEqual(0, po_mock.call_count)

    def test_get_juju_timings(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, 'my/juju/bin')
        client._backend.juju_timings = {("juju", "op1"): [1],
                                        ("juju", "op2"): [2]}
        flattened_timings = client.get_juju_timings()
        expected = {"juju op1": [1], "juju op2": [2]}
        self.assertEqual(flattened_timings, expected)

    def test_deployer(self):
        client = EnvJujuClient(JujuData('foo', {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'deployer', ('-e', 'foo:foo', '--debug', '--deploy-delay',
                         '10', '--timeout', '3600', '--config',
                         'bundle:~juju-qa/some-bundle'),
            True, include_e=False
        )

    def test_deployer_with_bundle_name(self):
        client = EnvJujuClient(JujuData('foo', {'type': 'local'}),
                               '2.0.0-series-arch', None)
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-bundle', 'name')
        mock.assert_called_with(
            'deployer', ('-e', 'foo:foo', '--debug', '--deploy-delay',
                         '10', '--timeout', '3600', '--config',
                         'bundle:~juju-qa/some-bundle', 'name'),
            True, include_e=False
        )

    def test_quickstart_maas(self):
        client = EnvJujuClient(JujuData(None, {'type': 'maas'}),
                               '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), False, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_local(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), True, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_nonlocal(self):
        client = EnvJujuClient(JujuData(None, {'type': 'nonlocal'}),
                               '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), False, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_template(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-{container}-bundle')
        mock.assert_called_with(
            'quickstart', (
                '--constraints', 'mem=2G', '--no-browser',
                'bundle:~juju-qa/some-lxd-bundle'),
            True, extra_env={'JUJU': '/juju'})

    def test_action_do(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'get_juju_output') as mock:
            mock.return_value = \
                "Action queued with id: 5a92ec93-d4be-4399-82dc-7431dbfd08f9"
            id = client.action_do("foo/0", "myaction", "param=5")
            self.assertEqual(id, "5a92ec93-d4be-4399-82dc-7431dbfd08f9")
        mock.assert_called_once_with(
            'run-action', 'foo/0', 'myaction', "param=5"
        )

    def test_action_do_error(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'get_juju_output') as mock:
            mock.return_value = "some bad text"
            with self.assertRaisesRegexp(Exception,
                                         "Action id not found in output"):
                client.action_do("foo/0", "myaction", "param=5")

    def test_action_fetch(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'get_juju_output') as mock:
            ret = "status: completed\nfoo: bar"
            mock.return_value = ret
            out = client.action_fetch("123")
            self.assertEqual(out, ret)
        mock.assert_called_once_with(
            'show-action-output', '123', "--wait", "1m"
        )

    def test_action_fetch_timeout(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        ret = "status: pending\nfoo: bar"
        with patch.object(EnvJujuClient,
                          'get_juju_output', return_value=ret):
            with self.assertRaisesRegexp(Exception,
                                         "timed out waiting for action"):
                client.action_fetch("123")

    def test_action_do_fetch(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(EnvJujuClient, 'get_juju_output') as mock:
            ret = "status: completed\nfoo: bar"
            # setting side_effect to an iterable will return the next value
            # from the list each time the function is called.
            mock.side_effect = [
                "Action queued with id: 5a92ec93-d4be-4399-82dc-7431dbfd08f9",
                ret]
            out = client.action_do_fetch("foo/0", "myaction", "param=5")
            self.assertEqual(out, ret)

    def test_run(self):
        client = fake_juju_client(cls=EnvJujuClient)
        run_list = [
            {"MachineId": "1",
             "Stdout": "Linux\n",
             "ReturnCode": 255,
             "Stderr": "Permission denied (publickey,password)"}]
        run_output = json.dumps(run_list)
        with patch.object(client._backend, 'get_juju_output',
                          return_value=run_output) as gjo_mock:
            result = client.run(('wname',), applications=['foo', 'bar'])
        self.assertEqual(run_list, result)
        gjo_mock.assert_called_once_with(
            'run', ('--format', 'json', '--application', 'foo,bar', 'wname'),
            frozenset(['migration']), 'foo',
            'name:name', user_name=None)

    def test_run_machines(self):
        client = fake_juju_client(cls=EnvJujuClient)
        output = json.dumps({"ReturnCode": 255})
        with patch.object(client, 'get_juju_output',
                          return_value=output) as output_mock:
            client.run(['true'], machines=['0', '1', '2'])
        output_mock.assert_called_once_with(
            'run', '--format', 'json', '--machine', '0,1,2', 'true')

    def test_run_use_json_false(self):
        client = fake_juju_client(cls=EnvJujuClient)
        output = json.dumps({"ReturnCode": 255})
        with patch.object(client, 'get_juju_output', return_value=output):
            result = client.run(['true'], use_json=False)
        self.assertEqual(output, result)

    def test_list_space(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        yaml_dict = {'foo': 'bar'}
        output = yaml.safe_dump(yaml_dict)
        with patch.object(client, 'get_juju_output', return_value=output,
                          autospec=True) as gjo_mock:
            result = client.list_space()
        self.assertEqual(result, yaml_dict)
        gjo_mock.assert_called_once_with('list-space')

    def test_add_space(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(client, 'juju', autospec=True) as juju_mock:
            client.add_space('foo-space')
        juju_mock.assert_called_once_with('add-space', ('foo-space'))

    def test_add_subnet(self):
        client = EnvJujuClient(JujuData(None, {'type': 'local'}),
                               '1.23-series-arch', None)
        with patch.object(client, 'juju', autospec=True) as juju_mock:
            client.add_subnet('bar-subnet', 'foo-space')
        juju_mock.assert_called_once_with('add-subnet',
                                          ('bar-subnet', 'foo-space'))

    def test__shell_environ_uses_pathsep(self):
        client = EnvJujuClient(JujuData('foo'), None, 'foo/bar/juju')
        with patch('os.pathsep', '!'):
            environ = client._shell_environ()
        self.assertRegexpMatches(environ['PATH'], r'foo/bar\!')

    def test_set_config(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.set_config('foo', {'bar': 'baz'})
        juju_mock.assert_called_once_with('config', ('foo', 'bar=baz'))

    def test_get_config(self):
        def output(*args, **kwargs):
            return yaml.safe_dump({
                'charm': 'foo',
                'service': 'foo',
                'settings': {
                    'dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
        expected = yaml.safe_load(output())
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'get_juju_output',
                          side_effect=output) as gjo_mock:
            results = client.get_config('foo')
        self.assertEqual(expected, results)
        gjo_mock.assert_called_once_with('config', 'foo')

    def test_get_service_config(self):
        def output(*args, **kwargs):
            return yaml.safe_dump({
                'charm': 'foo',
                'service': 'foo',
                'settings': {
                    'dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
        expected = yaml.safe_load(output())
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'get_juju_output', side_effect=output):
            results = client.get_service_config('foo')
        self.assertEqual(expected, results)

    def test_get_service_config_timesout(self):
        client = EnvJujuClient(JujuData('foo', {}), None, '/foo')
        with patch('jujupy.until_timeout', return_value=range(0)):
            with self.assertRaisesRegexp(
                    Exception, 'Timed out waiting for juju get'):
                client.get_service_config('foo')

    def test_upgrade_mongo(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_mongo()
        juju_mock.assert_called_once_with('upgrade-mongo', ())

    def test_enable_feature(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        self.assertEqual(set(), client.feature_flags)
        client.enable_feature('actions')
        self.assertEqual(set(['actions']), client.feature_flags)

    def test_enable_feature_invalid(self):
        client = EnvJujuClient(JujuData('bar', {}), None, '/foo')
        self.assertEqual(set(), client.feature_flags)
        with self.assertRaises(ValueError) as ctx:
            client.enable_feature('nomongo')
        self.assertEqual(str(ctx.exception), "Unknown feature flag: 'nomongo'")

    def test_is_juju1x(self):
        client = EnvJujuClient(None, '1.25.5', None)
        self.assertTrue(client.is_juju1x())

    def test_is_juju1x_false(self):
        client = EnvJujuClient(None, '2.0.0', None)
        self.assertFalse(client.is_juju1x())

    def test__get_register_command_returns_register_token(self):
        output = dedent("""\
        User "x" added
        User "x" granted read access to model "y"
        Please send this command to x:
            juju register AaBbCc""")
        output_cmd = 'AaBbCc'
        fake_client = fake_juju_client()

        register_cmd = fake_client._get_register_command(output)
        self.assertEqual(register_cmd, output_cmd)

    def test_revoke(self):
        fake_client = fake_juju_client()
        username = 'fakeuser'
        model = 'foo'
        default_permissions = 'read'
        default_model = fake_client.model_name
        default_controller = fake_client.env.controller.name

        with patch.object(fake_client, 'juju', return_value=True):
            fake_client.revoke(username)
            fake_client.juju.assert_called_with('revoke',
                                                ('-c', default_controller,
                                                 username, default_permissions,
                                                 default_model),
                                                include_e=False)

            fake_client.revoke(username, model)
            fake_client.juju.assert_called_with('revoke',
                                                ('-c', default_controller,
                                                 username, default_permissions,
                                                 model),
                                                include_e=False)

            fake_client.revoke(username, model, permissions='write')
            fake_client.juju.assert_called_with('revoke',
                                                ('-c', default_controller,
                                                 username, 'write', model),
                                                include_e=False)

    def test_add_user_perms(self):
        fake_client = fake_juju_client()
        username = 'fakeuser'

        # Ensure add_user returns expected value.
        self.assertEqual(
            fake_client.add_user_perms(username),
            get_user_register_token(username))

    @staticmethod
    def assert_add_user_perms(model, permissions):
        fake_client = fake_juju_client()
        username = 'fakeuser'
        output = get_user_register_command_info(username)
        if permissions is None:
            permissions = 'login'
        expected_args = [username, '-c', fake_client.env.controller.name]
        with patch.object(fake_client, 'get_juju_output',
                          return_value=output) as get_output:
            with patch.object(fake_client, 'juju') as mock_juju:
                fake_client.add_user_perms(username, model, permissions)
                if model is None:
                    model = fake_client.env.environment
                get_output.assert_called_with(
                    'add-user', *expected_args, include_e=False)
                if permissions == 'login':
                    mock_juju.assert_called_once_with(
                        'grant',
                        ('fakeuser', permissions,
                         '-c', fake_client.env.controller.name),
                        include_e=False)
                else:
                    mock_juju.assert_called_once_with(
                        'grant',
                        ('fakeuser', permissions,
                         model,
                         '-c', fake_client.env.controller.name),
                        include_e=False)

    def test_assert_add_user_permissions(self):
        model = 'foo'
        permissions = 'write'

        # Check using default model and permissions
        self.assert_add_user_perms(None, None)

        # Check explicit model & default permissions
        self.assert_add_user_perms(model, None)

        # Check explicit model & permissions
        self.assert_add_user_perms(model, permissions)

        # Check default model & explicit permissions
        self.assert_add_user_perms(None, permissions)

    def test_disable_user(self):
        env = JujuData('foo')
        username = 'fakeuser'
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.disable_user(username)
        mock.assert_called_with(
            'disable-user', ('-c', 'foo', 'fakeuser'), include_e=False)

    def test_enable_user(self):
        env = JujuData('foo')
        username = 'fakeuser'
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.enable_user(username)
        mock.assert_called_with(
            'enable-user', ('-c', 'foo', 'fakeuser'), include_e=False)

    def test_logout(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.logout()
        mock.assert_called_with(
            'logout', ('-c', 'foo'), include_e=False)

    def test_create_cloned_environment(self):
        fake_client = fake_juju_client()
        fake_client.bootstrap()
        # fake_client_environ = fake_client._shell_environ()
        controller_name = 'user_controller'
        cloned = fake_client.create_cloned_environment(
            'fakehome',
            controller_name
        )
        self.assertIs(fake_client.__class__, type(cloned))
        self.assertEqual(cloned.env.juju_home, 'fakehome')
        self.assertEqual(cloned.env.controller.name, controller_name)
        self.assertEqual(fake_client.env.controller.name, 'name')

    def test_list_clouds(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_juju_output') as mock:
            client.list_clouds()
        mock.assert_called_with(
            'list-clouds', '--format', 'json', include_e=False)

    def test_show_controller(self):
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_juju_output') as mock:
            client.show_controller()
        mock.assert_called_with(
            'show-controller', '--format', 'json', include_e=False)

    def test_show_machine(self):
        output = """\
        machines:
          "0":
            series: xenial
        """
        env = JujuData('foo')
        client = EnvJujuClient(env, None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=output) as mock:
            data = client.show_machine('0')
        mock.assert_called_once_with('show-machine', '0', '--format', 'yaml')
        self.assertEqual({'machines': {'0': {'series': 'xenial'}}}, data)

    def test_ssh_keys(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        given_output = 'ssh keys output'
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=given_output) as mock:
            output = client.ssh_keys()
        self.assertEqual(output, given_output)
        mock.assert_called_once_with('ssh-keys')

    def test_ssh_keys_full(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        given_output = 'ssh keys full output'
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=given_output) as mock:
            output = client.ssh_keys(full=True)
        self.assertEqual(output, given_output)
        mock.assert_called_once_with('ssh-keys', '--full')

    def test_add_ssh_key(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value='') as mock:
            output = client.add_ssh_key('ak', 'bk')
        self.assertEqual(output, '')
        mock.assert_called_once_with(
            'add-ssh-key', 'ak', 'bk', merge_stderr=True)

    def test_remove_ssh_key(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value='') as mock:
            output = client.remove_ssh_key('ak', 'bk')
        self.assertEqual(output, '')
        mock.assert_called_once_with(
            'remove-ssh-key', 'ak', 'bk', merge_stderr=True)

    def test_import_ssh_key(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value='') as mock:
            output = client.import_ssh_key('gh:au', 'lp:bu')
        self.assertEqual(output, '')
        mock.assert_called_once_with(
            'import-ssh-key', 'gh:au', 'lp:bu', merge_stderr=True)

    def test_list_disabled_commands(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=dedent("""\
             - command-set: destroy-model
               message: Lock Models
             - command-set: remove-object""")) as mock:
            output = client.list_disabled_commands()
        self.assertEqual([{'command-set': 'destroy-model',
                           'message': 'Lock Models'},
                          {'command-set': 'remove-object'}], output)
        mock.assert_called_once_with('list-disabled-commands',
                                     '--format', 'yaml')

    def test_disable_command(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'juju', autospec=True) as mock:
            client.disable_command(('all', 'message'))
        mock.assert_called_once_with('disable-command', ('all', 'message'))

    def test_enable_command(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'juju', autospec=True) as mock:
            client.enable_command('all')
        mock.assert_called_once_with('enable-command', 'all')

    def test_sync_tools(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'juju', autospec=True) as mock:
            client.sync_tools()
        mock.assert_called_once_with('sync-tools', ())

    def test_sync_tools_local_dir(self):
        client = EnvJujuClient(JujuData('foo'), None, None)
        with patch.object(client, 'juju', autospec=True) as mock:
            client.sync_tools('/agents')
        mock.assert_called_once_with('sync-tools', ('--local-dir', '/agents'),
                                     include_e=False)


class TestEnvJujuClientRC(ClientTest):

    def test_bootstrap(self):
        env = JujuData('foo', {'type': 'bar', 'region': 'baz'})
        with observable_temp_file() as config_file:
            with patch.object(EnvJujuClientRC, 'juju') as mock:
                client = EnvJujuClientRC(env, '2.0-zeta1', None)
                client.bootstrap()
                mock.assert_called_with(
                    'bootstrap', ('--constraints', 'mem=2G',
                                  'foo', 'bar/baz',
                                  '--config', config_file.name,
                                  '--default-model', 'foo',
                                  '--agent-version', '2.0'), include_e=False)
                config_file.seek(0)
                config = yaml.safe_load(config_file)
        self.assertEqual({'test-mode': True}, config)


class TestEnvJujuClient1X(ClientTest):

    def test_raise_on_juju_data(self):
        env = JujuData('foo', {'type': 'bar'}, 'baz')
        with self.assertRaisesRegexp(
                IncompatibleConfigClass,
                'JujuData cannot be used with EnvJujuClient1X'):
            EnvJujuClient1X(env, '1.25', 'full_path')

    def test_no_duplicate_env(self):
        env = SimpleEnvironment('foo', {})
        client = EnvJujuClient1X(env, '1.25', 'full_path')
        self.assertIs(env, client.env)

    def test_not_supported(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {}), '1.25', 'full_path')
        with self.assertRaises(JESNotSupported):
            client.add_user_perms('test-user')
        with self.assertRaises(JESNotSupported):
            client.grant('test-user', 'read')
        with self.assertRaises(JESNotSupported):
            client.revoke('test-user', 'read')
        with self.assertRaises(JESNotSupported):
            client.get_model_uuid()

    def test_get_version(self):
        value = ' 5.6 \n'
        with patch('subprocess.check_output', return_value=value) as vsn:
            version = EnvJujuClient1X.get_version()
        self.assertEqual('5.6', version)
        vsn.assert_called_with(('juju', '--version'))

    def test_get_version_path(self):
        with patch('subprocess.check_output', return_value=' 4.3') as vsn:
            EnvJujuClient1X.get_version('foo/bar/baz')
        vsn.assert_called_once_with(('foo/bar/baz', '--version'))

    def test_get_matching_agent_version(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        self.assertEqual('1.23.1', client.get_matching_agent_version())
        self.assertEqual('1.23', client.get_matching_agent_version(
                         no_build=True))
        client = client.clone(version='1.20-beta1-series-arch')
        self.assertEqual('1.20-beta1.1', client.get_matching_agent_version())

    def test_upgrade_juju_nonlocal(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'nonlocal'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234'))

    def test_upgrade_juju_local(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju()
        juju_mock.assert_called_with(
            'upgrade-juju', ('--version', '1.234', '--upload-tools',))

    def test_upgrade_juju_no_force_version(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as juju_mock:
            client.upgrade_juju(force_version=False)
        juju_mock.assert_called_with(
            'upgrade-juju', ('--upload-tools',))

    def test_upgrade_mongo_exception(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with self.assertRaises(UpgradeMongoNotSupported):
            client.upgrade_mongo()

    def test_get_cache_path(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', juju_home='/foo/'),
                                 '1.27', 'full/path', debug=True)
        self.assertEqual('/foo/environments/cache.yaml',
                         client.get_cache_path())

    def test_full_args(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin')
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual(('bin', '--show-log', 'bar', '-e', 'foo', 'baz',
                          'qux'), full)
        full = client._full_args('bar', True, ('baz', 'qux'))
        self.assertEqual((
            'bin', '--show-log', 'bar', '-e', 'foo',
            'baz', 'qux'), full)
        client.env = None
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual(('bin', '--show-log', 'bar', 'baz', 'qux'), full)

    def test_full_args_debug(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin', debug=True)
        full = client._full_args('bar', False, ('baz', 'qux'))
        self.assertEqual((
            'bin', '--debug', 'bar', '-e', 'foo', 'baz', 'qux'), full)

    def test_full_args_controller(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin')
        full = client._full_args('bar', False, ('baz', 'qux'), controller=True)
        self.assertEqual((
            'bin', '--show-log', 'bar', '-e', 'foo', 'baz', 'qux'), full)

    def test_full_args_action(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin')
        full = client._full_args('action bar', False, ('baz', 'qux'))
        self.assertEqual((
            'bin', '--show-log', 'action', 'bar', '-e', 'foo', 'baz', 'qux'),
            full)

    def test_bootstrap_maas(self):
        env = SimpleEnvironment('maas')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client = EnvJujuClient1X(env, None, None)
            with patch.object(client.env, 'maas', lambda: True):
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), False)

    def test_bootstrap_joyent(self):
        env = SimpleEnvironment('joyent')
        with patch.object(EnvJujuClient1X, 'juju', autospec=True) as mock:
            client = EnvJujuClient1X(env, None, None)
            with patch.object(client.env, 'joyent', lambda: True):
                client.bootstrap()
            mock.assert_called_once_with(
                client, 'bootstrap', ('--constraints', 'mem=2G cpu-cores=1'),
                False)

    def test_bootstrap_non_sudo(self):
        env = SimpleEnvironment('foo')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client = EnvJujuClient1X(env, None, None)
            with patch.object(client.env, 'needs_sudo', lambda: False):
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), False)

    def test_bootstrap_sudo(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.bootstrap()
            mock.assert_called_with(
                'bootstrap', ('--constraints', 'mem=2G'), True)

    def test_bootstrap_upload_tools(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.bootstrap(upload_tools=True)
            mock.assert_called_with(
                'bootstrap', ('--upload-tools', '--constraints', 'mem=2G'),
                True)

    def test_bootstrap_args(self):
        env = SimpleEnvironment('foo', {})
        client = EnvJujuClient1X(env, None, None)
        with self.assertRaisesRegexp(
                BootstrapMismatch,
                '--bootstrap-series angsty does not match default-series:'
                ' None'):
            client.bootstrap(bootstrap_series='angsty')
        env.config.update({
            'default-series': 'angsty',
            })
        with patch.object(client, 'juju') as mock:
            client.bootstrap(bootstrap_series='angsty')
        mock.assert_called_with(
            'bootstrap', ('--constraints', 'mem=2G'),
            False)

    def test_bootstrap_async(self):
        env = SimpleEnvironment('foo')
        with patch.object(EnvJujuClient, 'juju_async', autospec=True) as mock:
            client = EnvJujuClient1X(env, None, None)
            client.env.juju_home = 'foo'
            with client.bootstrap_async():
                mock.assert_called_once_with(
                    client, 'bootstrap', ('--constraints', 'mem=2G'))

    def test_bootstrap_async_upload_tools(self):
        env = SimpleEnvironment('foo')
        with patch.object(EnvJujuClient, 'juju_async', autospec=True) as mock:
            client = EnvJujuClient1X(env, None, None)
            with client.bootstrap_async(upload_tools=True):
                mock.assert_called_with(
                    client, 'bootstrap', ('--upload-tools', '--constraints',
                                          'mem=2G'))

    def test_get_bootstrap_args_bootstrap_series(self):
        env = SimpleEnvironment('foo', {})
        client = EnvJujuClient1X(env, None, None)
        with self.assertRaisesRegexp(
                BootstrapMismatch,
                '--bootstrap-series angsty does not match default-series:'
                ' None'):
            client.get_bootstrap_args(upload_tools=True,
                                      bootstrap_series='angsty')
        env.config['default-series'] = 'angsty'
        args = client.get_bootstrap_args(upload_tools=True,
                                         bootstrap_series='angsty')
        self.assertEqual(args, ('--upload-tools', '--constraints', 'mem=2G'))

    def test_create_environment_system(self):
        self.do_create_environment(
            'system', 'system create-environment', ('-s', 'foo'))

    def test_create_environment_controller(self):
        self.do_create_environment(
            'controller', 'controller create-environment', ('-c', 'foo'))

    def test_create_environment_hypenated_controller(self):
        self.do_create_environment(
            'kill-controller', 'create-environment', ('-c', 'foo'))

    def do_create_environment(self, jes_command, create_cmd,
                              controller_option):
        controller_client = EnvJujuClient1X(SimpleEnvironment('foo'), '1.26.1',
                                            None)
        model_env = SimpleEnvironment('bar', {'type': 'foo'})
        with patch.object(controller_client, 'get_jes_command',
                          return_value=jes_command):
            with patch.object(controller_client, 'juju') as juju_mock:
                with observable_temp_file() as config_file:
                    controller_client.add_model(model_env)
        juju_mock.assert_called_once_with(
            create_cmd, controller_option + (
                'bar', '--config', config_file.name), include_e=False)

    def test_destroy_environment_non_sudo(self):
        env = SimpleEnvironment('foo', {'type': 'gce'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: False):
            with patch.object(client, 'juju') as mock:
                client.destroy_environment()
            mock.assert_called_with(
                'destroy-environment', ('foo', '--force', '-y'),
                False, check=False, include_e=False, timeout=600)

    def test_destroy_environment_sudo(self):
        env = SimpleEnvironment('foo', {'type': 'gce'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client.env, 'needs_sudo', lambda: True):
            with patch.object(client, 'juju') as mock:
                client.destroy_environment()
            mock.assert_called_with(
                'destroy-environment', ('foo', '--force', '-y'),
                True, check=False, include_e=False, timeout=600)

    def test_destroy_environment_no_force(self):
        env = SimpleEnvironment('foo', {'type': 'gce'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.destroy_environment(force=False)
            mock.assert_called_with(
                'destroy-environment', ('foo', '-y'),
                False, check=False, include_e=False, timeout=600)

    def test_destroy_environment_azure(self):
        env = SimpleEnvironment('foo', {'type': 'azure'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.destroy_environment(force=False)
            mock.assert_called_with(
                'destroy-environment', ('foo', '-y'),
                False, check=False, include_e=False, timeout=1800)

    def test_destroy_environment_delete_jenv(self):
        env = SimpleEnvironment('foo', {'type': 'gce'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'juju'):
            with temp_env({}) as juju_home:
                client.env.juju_home = juju_home
                jenv_path = get_jenv_path(juju_home, 'foo')
                os.makedirs(os.path.dirname(jenv_path))
                open(jenv_path, 'w')
                self.assertTrue(os.path.exists(jenv_path))
                client.destroy_environment(delete_jenv=True)
                self.assertFalse(os.path.exists(jenv_path))

    def test_destroy_model(self):
        env = SimpleEnvironment('foo', {'type': 'gce'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'juju') as mock:
            client.destroy_model()
        mock.assert_called_with(
            'destroy-environment', ('foo', '-y'),
            False, check=False, include_e=False, timeout=600)

    def test_kill_controller(self):
        self.do_kill_controller('kill-controller', 'kill-controller')

    def do_kill_controller(self, jes_command, kill_command):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'gce'}), None, None)
        with patch.object(client, 'get_jes_command',
                          return_value=jes_command) as jes_mock:
            with patch.object(client, 'juju') as juju_mock:
                client.kill_controller()
        juju_mock.assert_called_once_with(
            'destroy-environment', ('foo', '--force', '-y'), check=False,
            include_e=False, timeout=600)
        self.assertEqual(0, jes_mock.call_count())

    def test_get_juju_output(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'juju')
        fake_popen = FakePopen('asdf', None, 0)
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_juju_output('bar')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-e', 'foo'),),
                         mock.call_args[0])

    def test_get_juju_output_accepts_varargs(self):
        env = SimpleEnvironment('foo')
        fake_popen = FakePopen('asdf', None, 0)
        client = EnvJujuClient1X(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_juju_output('bar', 'baz', '--qux')
        self.assertEqual('asdf', result)
        self.assertEqual((('juju', '--show-log', 'bar', '-e', 'foo', 'baz',
                           '--qux'),), mock.call_args[0])

    def test_get_juju_output_stderr(self):
        env = SimpleEnvironment('foo')
        fake_popen = FakePopen('Hello', 'Error!', 1)
        client = EnvJujuClient1X(env, None, 'juju')
        with self.assertRaises(subprocess.CalledProcessError) as exc:
            with patch('subprocess.Popen', return_value=fake_popen):
                client.get_juju_output('bar')
        self.assertEqual(exc.exception.output, 'Hello')
        self.assertEqual(exc.exception.stderr, 'Error!')

    def test_get_juju_output_full_cmd(self):
        env = SimpleEnvironment('foo')
        fake_popen = FakePopen(None, 'Hello!', 1)
        client = EnvJujuClient1X(env, None, 'juju')
        with self.assertRaises(subprocess.CalledProcessError) as exc:
            with patch('subprocess.Popen', return_value=fake_popen):
                client.get_juju_output('bar', '--baz', 'qux')
        self.assertEqual(
            ('juju', '--show-log', 'bar', '-e', 'foo', '--baz', 'qux'),
            exc.exception.cmd)

    def test_get_juju_output_accepts_timeout(self):
        env = SimpleEnvironment('foo')
        fake_popen = FakePopen('asdf', None, 0)
        client = EnvJujuClient1X(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as po_mock:
            client.get_juju_output('bar', timeout=5)
        self.assertEqual(
            po_mock.call_args[0][0],
            (sys.executable, get_timeout_path(), '5.00', '--', 'juju',
             '--show-log', 'bar', '-e', 'foo'))

    def test__shell_environ_juju_home(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('baz', {'type': 'ec2'}), '1.25-foobar', 'path',
            'asdf')
        env = client._shell_environ()
        self.assertEqual(env['JUJU_HOME'], 'asdf')
        self.assertNotIn('JUJU_DATA', env)

    def test_juju_output_supplies_path(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, '/foobar/bar')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
            return FakePopen(None, None, 0)
        with patch('subprocess.Popen', autospec=True,
                   side_effect=check_path):
            client.get_juju_output('cmd', 'baz')

    def test_get_status(self):
        output_text = dedent("""\
                - a
                - b
                - c
                """)
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=output_text) as gjo_mock:
            result = client.get_status()
        gjo_mock.assert_called_once_with(
            'status', '--format', 'yaml', controller=False)
        self.assertEqual(ServiceStatus, type(result))
        self.assertEqual(['a', 'b', 'c'], result.status)

    def test_get_status_retries_on_error(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        client.attempt = 0

        def get_juju_output(command, *args, **kwargs):
            if client.attempt == 1:
                return '"hello"'
            client.attempt += 1
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output', get_juju_output):
            client.get_status()

    def test_get_status_raises_on_timeout_1(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)

        def get_juju_output(command, *args, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        with patch.object(client, 'get_juju_output',
                          side_effect=get_juju_output):
            with patch('jujupy.until_timeout', lambda x: iter([None, None])):
                with self.assertRaisesRegexp(
                        Exception, 'Timed out waiting for juju status'):
                    client.get_status()

    def test_get_status_raises_on_timeout_2(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch('jujupy.until_timeout', return_value=iter([1])) as mock_ut:
            with patch.object(client, 'get_juju_output',
                              side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    client.get_status(500)
        mock_ut.assert_called_with(500)

    def test_get_status_controller(self):
        output_text = """\
            - a
            - b
            - c
        """
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'get_juju_output',
                          return_value=output_text) as gjo_mock:
            client.get_status(controller=True)
        gjo_mock.assert_called_once_with(
            'status', '--format', 'yaml', controller=True)

    @staticmethod
    def make_status_yaml(key, machine_value, unit_value):
        return dedent("""\
            machines:
              "0":
                {0}: {1}
            services:
              jenkins:
                units:
                  jenkins/0:
                    {0}: {2}
        """.format(key, machine_value, unit_value))

    def test_deploy_non_joyent(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_joyent(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb')
        mock_juju.assert_called_with('deploy', ('mondogb',))

    def test_deploy_repository(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', '/home/jrandom/repo')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--repository', '/home/jrandom/repo'))

    def test_deploy_to(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('mondogb', to='0')
        mock_juju.assert_called_with(
            'deploy', ('mondogb', '--to', '0'))

    def test_deploy_service(self):
        env = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(env, 'juju') as mock_juju:
            env.deploy('local:mondogb', service='my-mondogb')
        mock_juju.assert_called_with(
            'deploy', ('local:mondogb', 'my-mondogb',))

    def test_upgrade_charm(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as mock_juju:
            client.upgrade_charm('foo-service',
                                 '/bar/repository/angsty/mongodb')
        mock_juju.assert_called_once_with(
            'upgrade-charm', ('foo-service', '--repository',
                              '/bar/repository',))

    def test_remove_service(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        with patch.object(client, 'juju') as mock_juju:
            client.remove_service('mondogb')
        mock_juju.assert_called_with('destroy-service', ('mondogb',))

    def test_status_until_always_runs_once(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        status_txt = self.make_status_yaml('agent-state', 'started', 'started')
        with patch.object(client, 'get_juju_output', return_value=status_txt):
            result = list(client.status_until(-1))
        self.assertEqual(
            [r.status for r in result], [Status.from_text(status_txt).status])

    def test_status_until_timeout(self):
        client = EnvJujuClient1X(
            SimpleEnvironment('foo', {'type': 'local'}), '1.234-76', None)
        status_txt = self.make_status_yaml('agent-state', 'started', 'started')
        status_yaml = yaml.safe_load(status_txt)

        def until_timeout_stub(timeout, start=None):
            return iter([None, None])

        with patch.object(client, 'get_juju_output', return_value=status_txt):
            with patch('jujupy.until_timeout',
                       side_effect=until_timeout_stub) as ut_mock:
                result = list(client.status_until(30, 70))
        self.assertEqual(
            [r.status for r in result], [status_yaml] * 3)
        # until_timeout is called by status as well as status_until.
        self.assertEqual(ut_mock.mock_calls,
                         [call(60), call(30, start=70), call(60), call(60)])

    def test_add_ssh_machines(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, 'juju')
        with patch('subprocess.check_call', autospec=True) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-bar'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-baz'), 2)
        self.assertEqual(cc_mock.call_count, 3)

    def test_add_ssh_machines_retry(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, 'juju')
        with patch('subprocess.check_call', autospec=True,
                   side_effect=[subprocess.CalledProcessError(None, None),
                                None, None, None]) as cc_mock:
            client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        self.pause_mock.assert_called_once_with(30)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 1)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-bar'), 2)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-baz'), 3)
        self.assertEqual(cc_mock.call_count, 4)

    def test_add_ssh_machines_fail_on_second_machine(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, 'juju')
        with patch('subprocess.check_call', autospec=True, side_effect=[
                None, subprocess.CalledProcessError(None, None), None, None
                ]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-bar'), 1)
        self.assertEqual(cc_mock.call_count, 2)

    def test_add_ssh_machines_fail_on_second_attempt(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, 'juju')
        with patch('subprocess.check_call', autospec=True, side_effect=[
                subprocess.CalledProcessError(None, None),
                subprocess.CalledProcessError(None, None)]) as cc_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                client.add_ssh_machines(['m-foo', 'm-bar', 'm-baz'])
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-machine', '-e', 'foo', 'ssh:m-foo'), 1)
        self.assertEqual(cc_mock.call_count, 2)

    def test_wait_for_started(self):
        value = self.make_status_yaml('agent-state', 'started', 'started')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_started()

    def test_wait_for_started_timeout(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x, start=None: range(1)):
            with patch.object(client, 'get_juju_output', return_value=value):
                writes = []
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for agents to start in local'):
                        client.wait_for_started()
                self.assertEqual(writes, ['pending: 0', ' .', '\n'])

    def test_wait_for_started_start(self):
        value = self.make_status_yaml('agent-state', 'started', 'pending')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                writes = []
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for agents to start in local'):
                        client.wait_for_started(start=now - timedelta(1200))
                self.assertEqual(writes, ['pending: jenkins/0', '\n'])

    def test_wait_for_started_logs_status(self):
        value = self.make_status_yaml('agent-state', 'pending', 'started')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            writes = []
            with patch.object(GroupReporter, '_write', autospec=True,
                              side_effect=lambda _, s: writes.append(s)):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_started(0)
            self.assertEqual(writes, ['pending: 0', '\n'])
        self.assertEqual(self.log_stream.getvalue(), 'ERROR %s\n' % value)

    def test_wait_for_subordinate_units(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1/0:
                        agent-state: started
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub2/0:
                        agent-state: started
                      sub3/0:
                        agent-state: started
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'jenkins', 'sub1', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_multiple_subordinate_units(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              ubuntu:
                units:
                  ubuntu/0:
                    subordinates:
                      sub/0:
                        agent-state: started
                  ubuntu/1:
                    subordinates:
                      sub/1:
                        agent-state: started
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch('jujupy.GroupReporter.update') as update_mock:
                    with patch('jujupy.GroupReporter.finish') as finish_mock:
                        client.wait_for_subordinate_units(
                            'ubuntu', 'sub', start=now - timedelta(1200))
        self.assertEqual([], update_mock.call_args_list)
        finish_mock.assert_called_once_with()

    def test_wait_for_subordinate_units_checks_slash_in_unit_name(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    subordinates:
                      sub1:
                        agent-state: started
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_subordinate_units(
                        'jenkins', 'sub1', start=now - timedelta(1200))

    def test_wait_for_subordinate_units_no_subordinate(self):
        value = dedent("""\
            machines:
              "0":
                agent-state: started
            services:
              jenkins:
                units:
                  jenkins/0:
                    agent-state: started
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        now = datetime.now() + timedelta(days=1)
        with patch('utility.until_timeout.now', return_value=now):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for agents to start in local'):
                    client.wait_for_subordinate_units(
                        'jenkins', 'sub1', start=now - timedelta(1200))

    def test_wait_for_workload(self):
        initial_status = ServiceStatus.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    workload-status:
                      current: waiting
                  subordinates:
                    ntp/0:
                      workload-status:
                        current: unknown
        """)
        final_status = Status(copy.deepcopy(initial_status.status), None)
        final_status.status['services']['jenkins']['units']['jenkins/0'][
            'workload-status']['current'] = 'active'
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[1]):
            with patch.object(client, 'get_status', autospec=True,
                              side_effect=[initial_status, final_status]):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads()
        self.assertEqual(writes, ['waiting: jenkins/0', '\n'])

    def test_wait_for_workload_all_unknown(self):
        status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    workload-status:
                      current: unknown
                  subordinates:
                    ntp/0:
                      workload-status:
                        current: unknown
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[]):
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads(timeout=1)
        self.assertEqual(writes, [])

    def test_wait_for_workload_no_workload_status(self):
        status = Status.from_text("""\
            services:
              jenkins:
                units:
                  jenkins/0:
                    agent-state: active
        """)
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        writes = []
        with patch('utility.until_timeout', autospec=True, return_value=[]):
            with patch.object(client, 'get_status', autospec=True,
                              return_value=status):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    client.wait_for_workloads(timeout=1)
        self.assertEqual(writes, [])

    def test_wait_for_ha(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'has-vote'},
                '1': {'state-server-member-status': 'has-vote'},
                '2': {'state-server-member-status': 'has-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_ha()

    def test_wait_for_ha_no_has_vote(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'no-vote'},
                '1': {'state-server-member-status': 'no-vote'},
                '2': {'state-server-member-status': 'no-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            writes = []
            with patch('jujupy.until_timeout', autospec=True,
                       return_value=[2, 1]):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception,
                            'Timed out waiting for voting to be enabled.'):
                        client.wait_for_ha()
            self.assertEqual(writes[:2], ['no-vote: 0, 1, 2', ' .'])
            self.assertEqual(writes[2:-1], ['.'] * (len(writes) - 3))
            self.assertEqual(writes[-1:], ['\n'])

    def test_wait_for_ha_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'state-server-member-status': 'has-vote'},
                '1': {'state-server-member-status': 'has-vote'},
            },
            'services': {},
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for voting to be enabled.'):
                    client.wait_for_ha()

    def test_wait_for_deploy_started(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'services': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'baz': 'qux'}
                    }
                }
            }
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_deploy_started()

    def test_wait_for_deploy_started_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
            'services': {},
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch('jujupy.until_timeout', lambda x: range(0)):
            with patch.object(client, 'get_juju_output', return_value=value):
                with self.assertRaisesRegexp(
                        Exception,
                        'Timed out waiting for services to start.'):
                    client.wait_for_deploy_started()

    def test_wait_for_version(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_timeout(self):
        value = self.make_status_yaml('agent-version', '1.17.2', '1.17.1')
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        writes = []
        with patch('jujupy.until_timeout', lambda x, start=None: [x]):
            with patch.object(client, 'get_juju_output', return_value=value):
                with patch.object(GroupReporter, '_write', autospec=True,
                                  side_effect=lambda _, s: writes.append(s)):
                    with self.assertRaisesRegexp(
                            Exception, 'Some versions did not update'):
                        client.wait_for_version('1.17.2')
        self.assertEqual(writes, ['1.17.1: jenkins/0', ' .', '\n'])

    def test_wait_for_version_handles_connection_error(self):
        err = subprocess.CalledProcessError(2, 'foo')
        err.stderr = 'Unable to connect to environment'
        err = CannotConnectEnv(err)
        status = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        actions = [err, status]

        def get_juju_output_fake(*args, **kwargs):
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            else:
                return action

        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', get_juju_output_fake):
            client.wait_for_version('1.17.2')

    def test_wait_for_version_raises_non_connection_error(self):
        err = Exception('foo')
        status = self.make_status_yaml('agent-version', '1.17.2', '1.17.2')
        actions = [err, status]

        def get_juju_output_fake(*args, **kwargs):
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            else:
                return action

        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', get_juju_output_fake):
            with self.assertRaisesRegexp(Exception, 'foo'):
                client.wait_for_version('1.17.2')

    def test_wait_for_just_machine_0(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
            },
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value):
            client.wait_for('machines-not-0', 'none')

    def test_wait_for_just_machine_0_timeout(self):
        value = yaml.safe_dump({
            'machines': {
                '0': {'agent-state': 'started'},
                '1': {'agent-state': 'started'},
            },
        })
        client = EnvJujuClient1X(SimpleEnvironment('local'), None, None)
        with patch.object(client, 'get_juju_output', return_value=value), \
            patch('jujupy.until_timeout', lambda x: range(0)), \
            self.assertRaisesRegexp(
                Exception,
                'Timed out waiting for machines-not-0'):
            client.wait_for('machines-not-0', 'none')

    def test_set_model_constraints(self):
        client = EnvJujuClient1X(SimpleEnvironment('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.set_model_constraints({'bar': 'baz'})
        juju_mock.assert_called_once_with('set-constraints', ('bar=baz',))

    def test_get_model_config(self):
        env = SimpleEnvironment('foo', None)
        fake_popen = FakePopen(yaml.safe_dump({'bar': 'baz'}), None, 0)
        client = EnvJujuClient1X(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as po_mock:
            result = client.get_model_config()
        assert_juju_call(
            self, po_mock, client, (
                'juju', '--show-log', 'get-env', '-e', 'foo'))
        self.assertEqual({'bar': 'baz'}, result)

    def test_get_env_option(self):
        env = SimpleEnvironment('foo', None)
        fake_popen = FakePopen('https://example.org/juju/tools', None, 0)
        client = EnvJujuClient1X(env, None, 'juju')
        with patch('subprocess.Popen', return_value=fake_popen) as mock:
            result = client.get_env_option('tools-metadata-url')
        self.assertEqual(
            mock.call_args[0][0],
            ('juju', '--show-log', 'get-env', '-e', 'foo',
             'tools-metadata-url'))
        self.assertEqual('https://example.org/juju/tools', result)

    def test_set_env_option(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'juju')
        with patch('subprocess.check_call') as mock:
            client.set_env_option(
                'tools-metadata-url', 'https://example.org/juju/tools')
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(
            ('juju', '--show-log', 'set-env', '-e', 'foo',
             'tools-metadata-url=https://example.org/juju/tools'))

    def test_unset_env_option(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'juju')
        with patch('subprocess.check_call') as mock:
            client.unset_env_option('tools-metadata-url')
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(
            ('juju', '--show-log', 'set-env', '-e', 'foo',
             'tools-metadata-url='))

    def test_set_testing_agent_metadata_url(self):
        env = SimpleEnvironment(None, {'type': 'foo'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                client.set_testing_agent_metadata_url()
        mock_get.assert_called_with('tools-metadata-url')
        mock_set.assert_called_with(
            'tools-metadata-url',
            'https://example.org/juju/testing/tools')

    def test_set_testing_agent_metadata_url_noop(self):
        env = SimpleEnvironment(None, {'type': 'foo'})
        client = EnvJujuClient1X(env, None, None)
        with patch.object(client, 'get_env_option') as mock_get:
            mock_get.return_value = 'https://example.org/juju/testing/tools'
            with patch.object(client, 'set_env_option') as mock_set:
                client.set_testing_agent_metadata_url()
        mock_get.assert_called_with('tools-metadata-url')
        self.assertEqual(0, mock_set.call_count)

    def test_juju(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, 'juju')
        with patch('subprocess.check_call') as mock:
            client.juju('foo', ('bar', 'baz'))
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        mock.assert_called_with(('juju', '--show-log', 'foo', '-e', 'qux',
                                 'bar', 'baz'))

    def test_juju_env(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
        with patch('subprocess.check_call', side_effect=check_path):
            client.juju('foo', ('bar', 'baz'))

    def test_juju_no_check(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, 'juju')
        environ = dict(os.environ)
        environ['JUJU_HOME'] = client.env.juju_home
        with patch('subprocess.call') as mock:
            client.juju('foo', ('bar', 'baz'), check=False)
        mock.assert_called_with(('juju', '--show-log', 'foo', '-e', 'qux',
                                 'bar', 'baz'))

    def test_juju_no_check_env(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')

        def check_path(*args, **kwargs):
            self.assertRegexpMatches(os.environ['PATH'], r'/foobar\:')
        with patch('subprocess.call', side_effect=check_path):
            client.juju('foo', ('bar', 'baz'), check=False)

    def test_juju_timeout(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.check_call') as cc_mock:
            client.juju('foo', ('bar', 'baz'), timeout=58)
        self.assertEqual(cc_mock.call_args[0][0], (
            sys.executable, get_timeout_path(), '58.00', '--', 'baz',
            '--show-log', 'foo', '-e', 'qux', 'bar', 'baz'))

    def test_juju_juju_home(self):
        env = SimpleEnvironment('qux')
        os.environ['JUJU_HOME'] = 'foo'
        client = EnvJujuClient1X(env, None, '/foobar/baz')

        def check_home(*args, **kwargs):
            self.assertEqual(os.environ['JUJU_HOME'], 'foo')
            yield
            self.assertEqual(os.environ['JUJU_HOME'], 'asdf')
            yield

        with patch('subprocess.check_call', side_effect=check_home):
            client.juju('foo', ('bar', 'baz'))
            client.env.juju_home = 'asdf'
            client.juju('foo', ('bar', 'baz'))

    def test_juju_extra_env(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, 'juju')
        extra_env = {'JUJU': '/juju', 'JUJU_HOME': client.env.juju_home}

        def check_env(*args, **kwargs):
            self.assertEqual('/juju', os.environ['JUJU'])

        with patch('subprocess.check_call', side_effect=check_env) as mock:
            client.juju('quickstart', ('bar', 'baz'), extra_env=extra_env)
        mock.assert_called_with(
            ('juju', '--show-log', 'quickstart', '-e', 'qux', 'bar', 'baz'))

    def test_juju_backup_with_tgz(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')

        def check_env(*args, **kwargs):
            self.assertEqual(os.environ['JUJU_ENV'], 'qux')
            return 'foojuju-backup-24.tgzz'
        with patch('subprocess.check_output',
                   side_effect=check_env) as co_mock:
            backup_file = client.backup()
        self.assertEqual(backup_file, os.path.abspath('juju-backup-24.tgz'))
        assert_juju_call(self, co_mock, client, ['juju', 'backup'])

    def test_juju_backup_with_tar_gz(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.check_output',
                   return_value='foojuju-backup-123-456.tar.gzbar'):
            backup_file = client.backup()
        self.assertEqual(
            backup_file, os.path.abspath('juju-backup-123-456.tar.gz'))

    def test_juju_backup_no_file(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.check_output', return_value=''):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                client.backup()

    def test_juju_backup_wrong_file(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.check_output',
                   return_value='mumu-backup-24.tgz'):
            with self.assertRaisesRegexp(
                    Exception, 'The backup file was not found in output'):
                client.backup()

    def test_juju_backup_environ(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        environ = client._shell_environ()
        environ['JUJU_ENV'] = client.env.environment

        def side_effect(*args, **kwargs):
            self.assertEqual(environ, os.environ)
            return 'foojuju-backup-123-456.tar.gzbar'
        with patch('subprocess.check_output', side_effect=side_effect):
            client.backup()
            self.assertNotEqual(environ, os.environ)

    def test_restore_backup(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch.object(client, 'get_juju_output') as gjo_mock:
            result = client.restore_backup('quxx')
        gjo_mock.assert_called_once_with('restore', '--constraints',
                                         'mem=2G', 'quxx')
        self.assertIs(gjo_mock.return_value, result)

    def test_restore_backup_async(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch.object(client, 'juju_async') as gjo_mock:
            result = client.restore_backup_async('quxx')
        gjo_mock.assert_called_once_with(
            'restore', ('--constraints', 'mem=2G', 'quxx'))
        self.assertIs(gjo_mock.return_value, result)

    def test_enable_ha(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch.object(client, 'juju', autospec=True) as eha_mock:
            client.enable_ha()
        eha_mock.assert_called_once_with('ensure-availability', ('-n', '3'))

    def test_juju_async(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.Popen') as popen_class_mock:
            with client.juju_async('foo', ('bar', 'baz')) as proc:
                assert_juju_call(self, popen_class_mock, client, (
                    'baz', '--show-log', 'foo', '-e', 'qux', 'bar', 'baz'))
                self.assertIs(proc, popen_class_mock.return_value)
                self.assertEqual(proc.wait.call_count, 0)
                proc.wait.return_value = 0
        proc.wait.assert_called_once_with()

    def test_juju_async_failure(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        with patch('subprocess.Popen') as popen_class_mock:
            with self.assertRaises(subprocess.CalledProcessError) as err_cxt:
                with client.juju_async('foo', ('bar', 'baz')):
                    proc_mock = popen_class_mock.return_value
                    proc_mock.wait.return_value = 23
        self.assertEqual(err_cxt.exception.returncode, 23)
        self.assertEqual(err_cxt.exception.cmd, (
            'baz', '--show-log', 'foo', '-e', 'qux', 'bar', 'baz'))

    def test_juju_async_environ(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        environ = client._shell_environ()
        proc_mock = Mock()
        with patch('subprocess.Popen') as popen_class_mock:

            def check_environ(*args, **kwargs):
                self.assertEqual(environ, os.environ)
                return proc_mock
            popen_class_mock.side_effect = check_environ
            proc_mock.wait.return_value = 0
            with client.juju_async('foo', ('bar', 'baz')):
                pass
            self.assertNotEqual(environ, os.environ)

    def test_is_jes_enabled(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        fake_popen = FakePopen(' %s' % SYSTEM, None, 0)
        with patch('subprocess.Popen',
                   return_value=fake_popen) as po_mock:
            self.assertIsFalse(client.is_jes_enabled())
        self.assertEqual(0, po_mock.call_count)

    def test_get_jes_command(self):
        env = SimpleEnvironment('qux')
        client = EnvJujuClient1X(env, None, '/foobar/baz')
        # Juju 1.24 and older do not have a JES command. It is an error
        # to call get_jes_command when is_jes_enabled is False
        fake_popen = FakePopen(' %s' % SYSTEM, None, 0)
        with patch('subprocess.Popen',
                   return_value=fake_popen) as po_mock:
            with self.assertRaises(JESNotSupported):
                client.get_jes_command()
        self.assertEqual(0, po_mock.call_count)

    def test_get_juju_timings(self):
        env = SimpleEnvironment('foo')
        client = EnvJujuClient1X(env, None, 'my/juju/bin')
        client._backend.juju_timings = {("juju", "op1"): [1],
                                        ("juju", "op2"): [2]}
        flattened_timings = client.get_juju_timings()
        expected = {"juju op1": [1], "juju op2": [2]}
        self.assertEqual(flattened_timings, expected)

    def test_deploy_bundle_1x(self):
        client = EnvJujuClient1X(SimpleEnvironment('an_env', None),
                                 '1.23-series-arch', None)
        with patch.object(client, 'juju') as mock_juju:
            client.deploy_bundle('bundle:~juju-qa/some-bundle')
        mock_juju.assert_called_with(
            'deployer', ('--debug', '--deploy-delay', '10', '--timeout',
                         '3600', '--config', 'bundle:~juju-qa/some-bundle'),
            False
        )

    def test_deploy_bundle_template(self):
        client = EnvJujuClient1X(SimpleEnvironment('an_env', None),
                                 '1.23-series-arch', None)
        with patch.object(client, 'juju') as mock_juju:
            client.deploy_bundle('bundle:~juju-qa/some-{container}-bundle')
        mock_juju.assert_called_with(
            'deployer', (
                '--debug', '--deploy-delay', '10', '--timeout', '3600',
                '--config', 'bundle:~juju-qa/some-lxc-bundle',
                ),
            False)

    def test_deployer(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'deployer', ('--debug', '--deploy-delay', '10', '--timeout',
                         '3600', '--config', 'bundle:~juju-qa/some-bundle'),
            True
        )

    def test_deployer_template(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-{container}-bundle')
        mock.assert_called_with(
            'deployer', (
                '--debug', '--deploy-delay', '10', '--timeout', '3600',
                '--config', 'bundle:~juju-qa/some-lxc-bundle',
                ), True
        )

    def test_deployer_with_bundle_name(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.deployer('bundle:~juju-qa/some-bundle', 'name')
        mock.assert_called_with(
            'deployer', ('--debug', '--deploy-delay', '10', '--timeout',
                         '3600', '--config', 'bundle:~juju-qa/some-bundle',
                         'name'),
            True
        )

    def test_quickstart_maas(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'maas'}),
                                 '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), False, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_local(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), True, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_nonlocal(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'nonlocal'}),
                                 '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-bundle')
        mock.assert_called_with(
            'quickstart',
            ('--constraints', 'mem=2G', '--no-browser',
             'bundle:~juju-qa/some-bundle'), False, extra_env={'JUJU': '/juju'}
        )

    def test_quickstart_template(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', '/juju')
        with patch.object(EnvJujuClient1X, 'juju') as mock:
            client.quickstart('bundle:~juju-qa/some-{container}-bundle')
        mock.assert_called_with(
            'quickstart', (
                '--constraints', 'mem=2G', '--no-browser',
                'bundle:~juju-qa/some-lxc-bundle'),
            True, extra_env={'JUJU': '/juju'})

    def test_list_models(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        client = EnvJujuClient1X(env, '1.23-series-arch', None)
        client.list_models()
        self.assertEqual(
            'INFO The model is environment foo\n',
            self.log_stream.getvalue())

    def test__get_models(self):
        data = """\
            - name: foo
              model-uuid: aaaa
            - name: bar
              model-uuid: bbbb
        """
        env = SimpleEnvironment('foo', {'type': 'local'})
        client = fake_juju_client(cls=EnvJujuClient1X, env=env)
        with patch.object(client, 'get_juju_output', return_value=data):
            models = client._get_models()
            self.assertEqual(
                [{'name': 'foo', 'model-uuid': 'aaaa'},
                 {'name': 'bar', 'model-uuid': 'bbbb'}],
                models)

    def test__get_models_exception(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        client = fake_juju_client(cls=EnvJujuClient1X, env=env)
        with patch.object(client, 'get_juju_output',
                          side_effect=subprocess.CalledProcessError('a', 'b')):
            self.assertEqual([], client._get_models())

    def test_get_models(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        client = EnvJujuClient1X(env, '1.23-series-arch', None)
        self.assertEqual({}, client.get_models())

    def test_iter_model_clients(self):
        data = """\
            - name: foo
              model-uuid: aaaa
              owner: admin@local
            - name: bar
              model-uuid: bbbb
              owner: admin@local
        """
        client = EnvJujuClient1X(SimpleEnvironment('foo', {}), None, None)
        with patch.object(client, 'get_juju_output', return_value=data):
            model_clients = list(client.iter_model_clients())
        self.assertEqual(2, len(model_clients))
        self.assertIs(client, model_clients[0])
        self.assertEqual('bar', model_clients[1].env.environment)

    def test_get_controller_client(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), {'bar': 'baz'},
                                 'myhome')
        controller_client = client.get_controller_client()
        self.assertIs(client, controller_client)

    def test_list_controllers(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        client = EnvJujuClient1X(env, '1.23-series-arch', None)
        client.list_controllers()
        self.assertEqual(
            'INFO The controller is environment foo\n',
            self.log_stream.getvalue())

    def test_get_controller_model_name(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        client = EnvJujuClient1X(env, '1.23-series-arch', None)
        controller_name = client.get_controller_model_name()
        self.assertEqual('foo', controller_name)

    def test_get_controller_endpoint_ipv4(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        client = EnvJujuClient1X(env, '1.23-series-arch', None)
        with patch.object(client, 'get_juju_output',
                          return_value='10.0.0.1:17070') as gjo_mock:
            endpoint = client.get_controller_endpoint()
        self.assertEqual('10.0.0.1', endpoint)
        gjo_mock.assert_called_once_with('api-endpoints')

    def test_get_controller_endpoint_ipv6(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        client = EnvJujuClient1X(env, '1.23-series-arch', None)
        with patch.object(client, 'get_juju_output',
                          return_value='[::1]:17070') as gjo_mock:
            endpoint = client.get_controller_endpoint()
        self.assertEqual('::1', endpoint)
        gjo_mock.assert_called_once_with('api-endpoints')

    def test_action_do(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'get_juju_output') as mock:
            mock.return_value = \
                "Action queued with id: 5a92ec93-d4be-4399-82dc-7431dbfd08f9"
            id = client.action_do("foo/0", "myaction", "param=5")
            self.assertEqual(id, "5a92ec93-d4be-4399-82dc-7431dbfd08f9")
        mock.assert_called_once_with(
            'action do', 'foo/0', 'myaction', "param=5"
        )

    def test_action_do_error(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'get_juju_output') as mock:
            mock.return_value = "some bad text"
            with self.assertRaisesRegexp(Exception,
                                         "Action id not found in output"):
                client.action_do("foo/0", "myaction", "param=5")

    def test_action_fetch(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'get_juju_output') as mock:
            ret = "status: completed\nfoo: bar"
            mock.return_value = ret
            out = client.action_fetch("123")
            self.assertEqual(out, ret)
        mock.assert_called_once_with(
            'action fetch', '123', "--wait", "1m"
        )

    def test_action_fetch_timeout(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        ret = "status: pending\nfoo: bar"
        with patch.object(EnvJujuClient1X,
                          'get_juju_output', return_value=ret):
            with self.assertRaisesRegexp(Exception,
                                         "timed out waiting for action"):
                client.action_fetch("123")

    def test_action_do_fetch(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(EnvJujuClient1X, 'get_juju_output') as mock:
            ret = "status: completed\nfoo: bar"
            # setting side_effect to an iterable will return the next value
            # from the list each time the function is called.
            mock.side_effect = [
                "Action queued with id: 5a92ec93-d4be-4399-82dc-7431dbfd08f9",
                ret]
            out = client.action_do_fetch("foo/0", "myaction", "param=5")
            self.assertEqual(out, ret)

    def test_run(self):
        env = SimpleEnvironment('name', {}, 'foo')
        client = fake_juju_client(cls=EnvJujuClient1X, env=env)
        run_list = [
            {"MachineId": "1",
             "Stdout": "Linux\n",
             "ReturnCode": 255,
             "Stderr": "Permission denied (publickey,password)"}]
        run_output = json.dumps(run_list)
        with patch.object(client._backend, 'get_juju_output',
                          return_value=run_output) as gjo_mock:
            result = client.run(('wname',), applications=['foo', 'bar'])
        self.assertEqual(run_list, result)
        gjo_mock.assert_called_once_with(
            'run', ('--format', 'json', '--service', 'foo,bar', 'wname'),
            frozenset(['migration']),
            'foo', 'name', user_name=None)

    def test_list_space(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        yaml_dict = {'foo': 'bar'}
        output = yaml.safe_dump(yaml_dict)
        with patch.object(client, 'get_juju_output', return_value=output,
                          autospec=True) as gjo_mock:
            result = client.list_space()
        self.assertEqual(result, yaml_dict)
        gjo_mock.assert_called_once_with('space list')

    def test_add_space(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(client, 'juju', autospec=True) as juju_mock:
            client.add_space('foo-space')
        juju_mock.assert_called_once_with('space create', ('foo-space'))

    def test_add_subnet(self):
        client = EnvJujuClient1X(SimpleEnvironment(None, {'type': 'local'}),
                                 '1.23-series-arch', None)
        with patch.object(client, 'juju', autospec=True) as juju_mock:
            client.add_subnet('bar-subnet', 'foo-space')
        juju_mock.assert_called_once_with('subnet add',
                                          ('bar-subnet', 'foo-space'))

    def test__shell_environ_uses_pathsep(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None,
                                 'foo/bar/juju')
        with patch('os.pathsep', '!'):
            environ = client._shell_environ()
        self.assertRegexpMatches(environ['PATH'], r'foo/bar\!')

    def test_set_config(self):
        client = EnvJujuClient1X(SimpleEnvironment('bar', {}), None, '/foo')
        with patch.object(client, 'juju') as juju_mock:
            client.set_config('foo', {'bar': 'baz'})
        juju_mock.assert_called_once_with('set', ('foo', 'bar=baz'))

    def test_get_config(self):
        def output(*args, **kwargs):
            return yaml.safe_dump({
                'charm': 'foo',
                'service': 'foo',
                'settings': {
                    'dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
        expected = yaml.safe_load(output())
        client = EnvJujuClient1X(SimpleEnvironment('bar', {}), None, '/foo')
        with patch.object(client, 'get_juju_output',
                          side_effect=output) as gjo_mock:
            results = client.get_config('foo')
        self.assertEqual(expected, results)
        gjo_mock.assert_called_once_with('get', 'foo')

    def test_get_service_config(self):
        def output(*args, **kwargs):
            return yaml.safe_dump({
                'charm': 'foo',
                'service': 'foo',
                'settings': {
                    'dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
        expected = yaml.safe_load(output())
        client = EnvJujuClient1X(SimpleEnvironment('bar', {}), None, '/foo')
        with patch.object(client, 'get_juju_output', side_effect=output):
            results = client.get_service_config('foo')
        self.assertEqual(expected, results)

    def test_get_service_config_timesout(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', {}), None, '/foo')
        with patch('jujupy.until_timeout', return_value=range(0)):
            with self.assertRaisesRegexp(
                    Exception, 'Timed out waiting for juju get'):
                client.get_service_config('foo')

    def test_ssh_keys(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', {}), None, None)
        given_output = 'ssh keys output'
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=given_output) as mock:
            output = client.ssh_keys()
        self.assertEqual(output, given_output)
        mock.assert_called_once_with('authorized-keys list')

    def test_ssh_keys_full(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', {}), None, None)
        given_output = 'ssh keys full output'
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=given_output) as mock:
            output = client.ssh_keys(full=True)
        self.assertEqual(output, given_output)
        mock.assert_called_once_with('authorized-keys list', '--full')

    def test_add_ssh_key(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', {}), None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value='') as mock:
            output = client.add_ssh_key('ak', 'bk')
        self.assertEqual(output, '')
        mock.assert_called_once_with(
            'authorized-keys add', 'ak', 'bk', merge_stderr=True)

    def test_remove_ssh_key(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', {}), None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value='') as mock:
            output = client.remove_ssh_key('ak', 'bk')
        self.assertEqual(output, '')
        mock.assert_called_once_with(
            'authorized-keys delete', 'ak', 'bk', merge_stderr=True)

    def test_import_ssh_key(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo', {}), None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value='') as mock:
            output = client.import_ssh_key('gh:au', 'lp:bu')
        self.assertEqual(output, '')
        mock.assert_called_once_with(
            'authorized-keys import', 'gh:au', 'lp:bu', merge_stderr=True)

    def test_list_disabled_commands(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, None)
        with patch.object(client, 'get_juju_output', autospec=True,
                          return_value=dedent("""\
             - command-set: destroy-model
               message: Lock Models
             - command-set: remove-object""")) as mock:
            output = client.list_disabled_commands()
        self.assertEqual([{'command-set': 'destroy-model',
                           'message': 'Lock Models'},
                          {'command-set': 'remove-object'}], output)
        mock.assert_called_once_with('block list',
                                     '--format', 'yaml')

    def test_disable_command(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, None)
        with patch.object(client, 'juju', autospec=True) as mock:
            client.disable_command(('all', 'message'))
        mock.assert_called_once_with('block', ('all', 'message'))

    def test_enable_command(self):
        client = EnvJujuClient1X(SimpleEnvironment('foo'), None, None)
        with patch.object(client, 'juju', autospec=True) as mock:
            client.enable_command('all')
        mock.assert_called_once_with('unblock', 'all')


class TestUniquifyLocal(TestCase):

    def test_uniquify_local_empty(self):
        env = SimpleEnvironment('foo', {'type': 'local'})
        uniquify_local(env)
        self.assertEqual(env.config, {
            'type': 'local',
            'api-port': 17071,
            'state-port': 37018,
            'storage-port': 8041,
            'syslog-port': 6515,
        })

    def test_uniquify_local_preset(self):
        env = SimpleEnvironment('foo', {
            'type': 'local',
            'api-port': 17071,
            'state-port': 37018,
            'storage-port': 8041,
            'syslog-port': 6515,
        })
        uniquify_local(env)
        self.assertEqual(env.config, {
            'type': 'local',
            'api-port': 17072,
            'state-port': 37019,
            'storage-port': 8042,
            'syslog-port': 6516,
        })

    def test_uniquify_nonlocal(self):
        env = SimpleEnvironment('foo', {
            'type': 'nonlocal',
            'api-port': 17071,
            'state-port': 37018,
            'storage-port': 8041,
            'syslog-port': 6515,
        })
        uniquify_local(env)
        self.assertEqual(env.config, {
            'type': 'nonlocal',
            'api-port': 17071,
            'state-port': 37018,
            'storage-port': 8041,
            'syslog-port': 6515,
        })


@contextmanager
def bootstrap_context(client=None):
    # Avoid unnecessary syscalls.
    with patch('jujupy.check_free_disk_space'):
        with scoped_environ():
            with temp_dir() as fake_home:
                os.environ['JUJU_HOME'] = fake_home
                yield fake_home


class TestJesHomePath(TestCase):

    def test_jes_home_path(self):
        path = jes_home_path('/home/jrandom/foo', 'bar')
        self.assertEqual(path, '/home/jrandom/foo/jes-homes/bar')


class TestGetCachePath(TestCase):

    def test_get_cache_path(self):
        path = get_cache_path('/home/jrandom/foo')
        self.assertEqual(path, '/home/jrandom/foo/environments/cache.yaml')

    def test_get_cache_path_models(self):
        path = get_cache_path('/home/jrandom/foo', models=True)
        self.assertEqual(path, '/home/jrandom/foo/models/cache.yaml')


def stub_bootstrap(client):
    jenv_path = get_jenv_path(client.env.juju_home, 'qux')
    os.mkdir(os.path.dirname(jenv_path))
    with open(jenv_path, 'w') as f:
        f.write('Bogus jenv')


class TestMakeSafeConfig(TestCase):

    def test_default(self):
        client = fake_juju_client(JujuData('foo', {'type': 'bar'},
                                           juju_home='foo'),
                                  version='1.2-alpha3-asdf-asdf')
        config = make_safe_config(client)
        self.assertEqual({
            'name': 'foo',
            'type': 'bar',
            'test-mode': True,
            'agent-version': '1.2-alpha3',
            }, config)

    def test_local(self):
        with temp_dir() as juju_home:
            env = JujuData('foo', {'type': 'local'}, juju_home=juju_home)
            client = fake_juju_client(env)
            with patch('jujupy.check_free_disk_space'):
                config = make_safe_config(client)
        self.assertEqual(get_local_root(client.env.juju_home, client.env),
                         config['root-dir'])

    def test_bootstrap_replaces_agent_version(self):
        client = fake_juju_client(JujuData('foo', {'type': 'bar'},
                                  juju_home='foo'))
        client.bootstrap_replaces = {'agent-version'}
        self.assertNotIn('agent-version', make_safe_config(client))
        client.env.config['agent-version'] = '1.23'
        self.assertNotIn('agent-version', make_safe_config(client))


class TestTempBootstrapEnv(FakeHomeTestCase):

    @staticmethod
    def get_client(env):
        return EnvJujuClient24(env, '1.24-fake', 'fake-juju-path')

    def test_no_config_mangling_side_effect(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            with temp_bootstrap_env(fake_home, client):
                stub_bootstrap(client)
        self.assertEqual(env.config, {'type': 'local'})

    def test_temp_bootstrap_env_environment(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        with bootstrap_context() as fake_home:
            client = self.get_client(env)
            agent_version = client.get_matching_agent_version()
            with temp_bootstrap_env(fake_home, client):
                temp_home = os.environ['JUJU_HOME']
                self.assertEqual(temp_home, os.environ['JUJU_DATA'])
                self.assertNotEqual(temp_home, fake_home)
                symlink_path = get_jenv_path(fake_home, 'qux')
                symlink_target = os.path.realpath(symlink_path)
                expected_target = os.path.realpath(
                    get_jenv_path(temp_home, 'qux'))
                self.assertEqual(symlink_target, expected_target)
                config = yaml.safe_load(
                    open(get_environments_path(temp_home)))
                self.assertEqual(config, {'environments': {'qux': {
                    'type': 'local',
                    'root-dir': get_local_root(fake_home, client.env),
                    'agent-version': agent_version,
                    'test-mode': True,
                    'name': 'qux',
                }}})
                stub_bootstrap(client)

    def test_temp_bootstrap_env_provides_dir(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        juju_home = os.path.join(self.home_dir, 'asdf')

        def side_effect(*args, **kwargs):
            os.mkdir(juju_home)
            return juju_home

        with patch('utility.mkdtemp', side_effect=side_effect):
            with patch('jujupy.check_free_disk_space', autospec=True):
                with temp_bootstrap_env(self.home_dir, client) as temp_home:
                    pass
        self.assertEqual(temp_home, juju_home)

    def test_temp_bootstrap_env_no_set_home(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        os.environ['JUJU_HOME'] = 'foo'
        os.environ['JUJU_DATA'] = 'bar'
        with patch('jujupy.check_free_disk_space', autospec=True):
            with temp_bootstrap_env(self.home_dir, client, set_home=False):
                self.assertEqual(os.environ['JUJU_HOME'], 'foo')
                self.assertEqual(os.environ['JUJU_DATA'], 'bar')

    def test_output(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            with temp_bootstrap_env(fake_home, client):
                stub_bootstrap(client)
            jenv_path = get_jenv_path(fake_home, 'qux')
            self.assertFalse(os.path.islink(jenv_path))
            self.assertEqual(open(jenv_path).read(), 'Bogus jenv')

    def test_rename_on_exception(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            with self.assertRaisesRegexp(Exception, 'test-rename'):
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap(client)
                    raise Exception('test-rename')
            jenv_path = get_jenv_path(os.environ['JUJU_HOME'], 'qux')
            self.assertFalse(os.path.islink(jenv_path))
            self.assertEqual(open(jenv_path).read(), 'Bogus jenv')

    def test_exception_no_jenv(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            with self.assertRaisesRegexp(Exception, 'test-rename'):
                with temp_bootstrap_env(fake_home, client):
                    jenv_path = get_jenv_path(os.environ['JUJU_HOME'], 'qux')
                    os.mkdir(os.path.dirname(jenv_path))
                    raise Exception('test-rename')
            jenv_path = get_jenv_path(os.environ['JUJU_HOME'], 'qux')
            self.assertFalse(os.path.lexists(jenv_path))

    def test_check_space_local_lxc(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        with bootstrap_context() as fake_home:
            client = self.get_client(env)
            with patch('jujupy.check_free_disk_space') as mock_cfds:
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap(client)
        self.assertEqual(mock_cfds.mock_calls, [
            call(os.path.join(fake_home, 'qux'), 8000000, 'MongoDB files'),
            call('/var/lib/lxc', 2000000, 'LXC containers'),
        ])

    def test_check_space_local_kvm(self):
        env = SimpleEnvironment('qux', {'type': 'local', 'container': 'kvm'})
        with bootstrap_context() as fake_home:
            client = self.get_client(env)
            with patch('jujupy.check_free_disk_space') as mock_cfds:
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap(client)
        self.assertEqual(mock_cfds.mock_calls, [
            call(os.path.join(fake_home, 'qux'), 8000000, 'MongoDB files'),
            call('/var/lib/uvtool/libvirt/images', 2000000, 'KVM disk files'),
        ])

    def test_error_on_jenv(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            jenv_path = get_jenv_path(fake_home, 'qux')
            os.mkdir(os.path.dirname(jenv_path))
            with open(jenv_path, 'w') as f:
                f.write('In the way')
            with self.assertRaisesRegexp(Exception, '.* already exists!'):
                with temp_bootstrap_env(fake_home, client):
                    stub_bootstrap(client)

    def test_not_permanent(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            client.env.juju_home = fake_home
            with temp_bootstrap_env(fake_home, client,
                                    permanent=False) as tb_home:
                stub_bootstrap(client)
            self.assertFalse(os.path.exists(tb_home))
            self.assertTrue(os.path.exists(get_jenv_path(fake_home,
                            client.env.environment)))
            self.assertFalse(os.path.exists(get_jenv_path(tb_home,
                             client.env.environment)))
        self.assertFalse(os.path.exists(tb_home))
        self.assertEqual(client.env.juju_home, fake_home)
        self.assertNotEqual(tb_home,
                            jes_home_path(fake_home, client.env.environment))

    def test_permanent(self):
        env = SimpleEnvironment('qux', {'type': 'local'})
        client = self.get_client(env)
        with bootstrap_context(client) as fake_home:
            client.env.juju_home = fake_home
            with temp_bootstrap_env(fake_home, client,
                                    permanent=True) as tb_home:
                stub_bootstrap(client)
            self.assertTrue(os.path.exists(tb_home))
            self.assertFalse(os.path.exists(get_jenv_path(fake_home,
                             client.env.environment)))
            self.assertTrue(os.path.exists(get_jenv_path(tb_home,
                            client.env.environment)))
        self.assertFalse(os.path.exists(tb_home))
        self.assertEqual(client.env.juju_home, tb_home)


class TestStatus(FakeHomeTestCase):

    def test_iter_machines_no_containers(self):
        status = Status({
            'machines': {
                '1': {'foo': 'bar', 'containers': {'1/lxc/0': {'baz': 'qux'}}}
            },
            'applications': {}}, '')
        self.assertEqual(list(status.iter_machines()),
                         [('1', status.status['machines']['1'])])

    def test_iter_machines_containers(self):
        status = Status({
            'machines': {
                '1': {'foo': 'bar', 'containers': {'1/lxc/0': {'baz': 'qux'}}}
            },
            'applications': {}}, '')
        self.assertEqual(list(status.iter_machines(containers=True)), [
            ('1', status.status['machines']['1']),
            ('1/lxc/0', {'baz': 'qux'}),
        ])

    def test__iter_units_in_application(self):
        status = Status({}, '')
        app_status = {
            'units': {'jenkins/1': {'subordinates': {'sub': {'baz': 'qux'}}}}
            }
        expected = [
            ('jenkins/1', {'subordinates': {'sub': {'baz': 'qux'}}}),
            ('sub', {'baz': 'qux'})]
        self.assertItemsEqual(expected,
                              status._iter_units_in_application(app_status))

    def test_agent_items_empty(self):
        status = Status({'machines': {}, 'applications': {}}, '')
        self.assertItemsEqual([], status.agent_items())

    def test_agent_items(self):
        status = Status({
            'machines': {
                '1': {'foo': 'bar'}
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'subordinates': {
                                'sub': {'baz': 'qux'}
                            }
                        }
                    }
                }
            }
        }, '')
        expected = [
            ('1', {'foo': 'bar'}),
            ('jenkins/1', {'subordinates': {'sub': {'baz': 'qux'}}}),
            ('sub', {'baz': 'qux'})]
        self.assertItemsEqual(expected, status.agent_items())

    def test_agent_items_containers(self):
        status = Status({
            'machines': {
                '1': {'foo': 'bar', 'containers': {
                    '2': {'qux': 'baz'},
                    }}
                },
            'applications': {}
            }, '')
        expected = [
            ('1', {'foo': 'bar', 'containers': {'2': {'qux': 'baz'}}}),
            ('2', {'qux': 'baz'})
            ]
        self.assertItemsEqual(expected, status.agent_items())

    def get_unit_agent_states_data(self):
        status = Status({
            'applications': {
                'jenkins': {
                    'units': {'jenkins/0': {'agent-state': 'good'},
                              'jenkins/1': {'agent-state': 'bad'}},
                    },
                'fakejob': {
                    'life': 'dying',
                    'units': {'fakejob/0': {'agent-state': 'good'}},
                    },
                }
            }, '')
        expected = {
            'good': ['jenkins/0'],
            'bad': ['jenkins/1'],
            'dying': ['fakejob/0'],
            }
        return status, expected

    def test_unit_agent_states_new(self):
        (status, expected) = self.get_unit_agent_states_data()
        actual = status.unit_agent_states()
        self.assertEqual(expected, actual)

    def test_unit_agent_states_existing(self):
        (status, expected) = self.get_unit_agent_states_data()
        actual = defaultdict(list)
        status.unit_agent_states(actual)
        self.assertEqual(expected, actual)

    def test_get_service_count_zero(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
                },
            }, '')
        self.assertEqual(0, status.get_service_count())

    def test_get_service_count(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                    }
                },
                'dummy-sink': {
                    'units': {
                        'dummy-sink/0': {'agent-state': 'started'},
                    }
                },
                'juju-reports': {
                    'units': {
                        'juju-reports/0': {'agent-state': 'pending'},
                    }
                }
            }
        }, '')
        self.assertEqual(3, status.get_service_count())

    def test_get_service_unit_count_zero(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
        }, '')
        self.assertEqual(0, status.get_service_unit_count('jenkins'))

    def test_get_service_unit_count(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                        'jenkins/2': {'agent-state': 'bad'},
                        'jenkins/3': {'agent-state': 'bad'},
                    }
                }
            }
        }, '')
        self.assertEqual(3, status.get_service_unit_count('jenkins'))

    def test_get_unit(self):
        status = Status({
            'machines': {
                '1': {},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                    }
                },
                'dummy-sink': {
                    'units': {
                        'jenkins/2': {'agent-state': 'started'},
                    }
                },
            }
        }, '')
        self.assertEqual(
            status.get_unit('jenkins/1'), {'agent-state': 'bad'})
        self.assertEqual(
            status.get_unit('jenkins/2'), {'agent-state': 'started'})
        with self.assertRaisesRegexp(KeyError, 'jenkins/3'):
            status.get_unit('jenkins/3')

    def test_service_subordinate_units(self):
        status = Status({
            'machines': {
                '1': {},
            },
            'applications': {
                'ubuntu': {},
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'subordinates': {
                                'chaos-monkey/0': {'agent-state': 'started'},
                            }
                        }
                    }
                },
                'dummy-sink': {
                    'units': {
                        'jenkins/2': {
                            'subordinates': {
                                'chaos-monkey/1': {'agent-state': 'started'}
                            }
                        },
                        'jenkins/3': {
                            'subordinates': {
                                'chaos-monkey/2': {'agent-state': 'started'}
                            }
                        }
                    }
                }
            }
        }, '')
        self.assertItemsEqual(
            status.service_subordinate_units('ubuntu'),
            [])
        self.assertItemsEqual(
            status.service_subordinate_units('jenkins'),
            [('chaos-monkey/0', {'agent-state': 'started'},)])
        self.assertItemsEqual(
            status.service_subordinate_units('dummy-sink'), [
                ('chaos-monkey/1', {'agent-state': 'started'}),
                ('chaos-monkey/2', {'agent-state': 'started'})]
            )

    def test_get_open_ports(self):
        status = Status({
            'machines': {
                '1': {},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                    }
                },
                'dummy-sink': {
                    'units': {
                        'jenkins/2': {'open-ports': ['42/tcp']},
                    }
                },
            }
        }, '')
        self.assertEqual(status.get_open_ports('jenkins/1'), [])
        self.assertEqual(status.get_open_ports('jenkins/2'), ['42/tcp'])

    def test_agent_states_with_agent_state(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                        'jenkins/2': {'agent-state': 'good'},
                    }
                }
            }
        }, '')
        expected = {
            'good': ['1', 'jenkins/2'],
            'bad': ['jenkins/1'],
            'no-agent': ['2'],
        }
        self.assertEqual(expected, status.agent_states())

    def test_agent_states_with_agent_status(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-status': {'current': 'bad'}},
                        'jenkins/2': {'agent-status': {'current': 'good'}},
                        'jenkins/3': {},
                    }
                }
            }
        }, '')
        expected = {
            'good': ['1', 'jenkins/2'],
            'bad': ['jenkins/1'],
            'no-agent': ['2', 'jenkins/3'],
        }
        self.assertEqual(expected, status.agent_states())

    def test_agent_states_with_juju_status(self):
        status = Status({
            'machines': {
                '1': {'juju-status': {'current': 'good'}},
                '2': {},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'juju-status': {'current': 'bad'}},
                        'jenkins/2': {'juju-status': {'current': 'good'}},
                        'jenkins/3': {},
                    }
                }
            }
        }, '')
        expected = {
            'good': ['1', 'jenkins/2'],
            'bad': ['jenkins/1'],
            'no-agent': ['2', 'jenkins/3'],
        }
        self.assertEqual(expected, status.agent_states())

    def test_agent_states_with_dying(self):
        status = Status({
            'machines': {},
            'applications': {
                'jenkins': {
                    'life': 'alive',
                    'units': {
                        'jenkins/1': {'juju-status': {'current': 'bad'}},
                        'jenkins/2': {'juju-status': {'current': 'good'}},
                        }
                    },
                'fakejob': {
                    'life': 'dying',
                    'units': {
                        'fakejob/1': {'juju-status': {'current': 'bad'}},
                        'fakejob/2': {'juju-status': {'current': 'good'}},
                        }
                    },
                }
            }, '')
        expected = {
            'good': ['jenkins/2'],
            'bad': ['jenkins/1'],
            'dying': ['fakejob/1', 'fakejob/2'],
            }
        self.assertEqual(expected, status.agent_states())

    def test_check_agents_started_not_started(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'good'},
                '2': {},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {'agent-state': 'bad'},
                        'jenkins/2': {'agent-state': 'good'},
                    }
                }
            }
        }, '')
        self.assertEqual(status.agent_states(),
                         status.check_agents_started('env1'))

    def test_check_agents_started_all_started_with_agent_state(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'started'},
                '2': {'agent-state': 'started'},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'agent-state': 'started',
                            'subordinates': {
                                'sub1': {
                                    'agent-state': 'started'
                                }
                            }
                        },
                        'jenkins/2': {'agent-state': 'started'},
                    }
                }
            }
        }, '')
        self.assertIsNone(status.check_agents_started('env1'))

    def test_check_agents_started_all_started_with_agent_status(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'started'},
                '2': {'agent-state': 'started'},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'agent-status': {'current': 'idle'},
                            'subordinates': {
                                'sub1': {
                                    'agent-status': {'current': 'idle'}
                                }
                            }
                        },
                        'jenkins/2': {'agent-status': {'current': 'idle'}},
                    }
                }
            }
        }, '')
        self.assertIsNone(status.check_agents_started('env1'))

    def test_check_agents_started_dying(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'started'},
                '2': {'agent-state': 'started'},
                },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/1': {
                            'agent-status': {'current': 'idle'},
                            'subordinates': {
                                'sub1': {
                                    'agent-status': {'current': 'idle'}
                                    }
                                }
                            },
                        'jenkins/2': {'agent-status': {'current': 'idle'}},
                        },
                    'life': 'dying',
                    }
                }
            }, '')
        self.assertEqual(status.agent_states(),
                         status.check_agents_started('env1'))

    def test_check_agents_started_agent_error(self):
        status = Status({
            'machines': {
                '1': {'agent-state': 'any-error'},
            },
            'applications': {}
        }, '')
        with self.assertRaisesRegexp(ErroredUnit,
                                     '1 is in state any-error'):
            status.check_agents_started('env1')

    def do_check_agents_started_agent_state_info_failure(self, failure):
        status = Status({
            'machines': {'0': {
                'agent-state-info': failure}},
            'applications': {},
        }, '')
        with self.assertRaises(ErroredUnit) as e_cxt:
            status.check_agents_started()
        e = e_cxt.exception
        self.assertEqual(
            str(e), '0 is in state {}'.format(failure))
        self.assertEqual(e.unit_name, '0')
        self.assertEqual(e.state, failure)

    def do_check_agents_started_juju_status_failure(self, failure):
        status = Status({
            'machines': {
                '0': {
                    'juju-status': {
                        'current': 'error',
                        'message': failure}
                    },
                }
            }, '')
        with self.assertRaises(ErroredUnit) as e_cxt:
            status.check_agents_started()
        e = e_cxt.exception
        # if message is blank, the failure should reflect the state instead
        if not failure:
            failure = 'error'
        self.assertEqual(
            str(e), '0 is in state {}'.format(failure))
        self.assertEqual(e.unit_name, '0')
        self.assertEqual(e.state, failure)

    def do_check_agents_started_info_and_status_failure(self, failure):
        status = Status({
            'machines': {
                '0': {
                    'agent-state-info': failure,
                    'juju-status': {
                        'current': 'error',
                        'message': failure}
                    },
                }
            }, '')
        with self.assertRaises(ErroredUnit) as e_cxt:
            status.check_agents_started()
        e = e_cxt.exception
        self.assertEqual(
            str(e), '0 is in state {}'.format(failure))
        self.assertEqual(e.unit_name, '0')
        self.assertEqual(e.state, failure)

    def test_check_agents_started_read_juju_status_error(self):
        failures = ['no "centos7" images in us-east-1 with arches [amd64]',
                    'sending new instance request: GCE operation ' +
                    '"operation-143" failed', '']
        for failure in failures:
            self.do_check_agents_started_juju_status_failure(failure)

    def test_check_agents_started_read_agent_state_info_error(self):
        failures = ['cannot set up groups foobar', 'cannot run instance',
                    'cannot run instances', 'error executing "lxc-start"']
        for failure in failures:
            self.do_check_agents_started_agent_state_info_failure(failure)

    def test_check_agents_started_agent_info_error(self):
        # Sometimes the error is indicated in a special 'agent-state-info'
        # field.
        status = Status({
            'machines': {
                '1': {'agent-state-info': 'any-error'},
            },
            'applications': {}
        }, '')
        with self.assertRaisesRegexp(ErroredUnit,
                                     '1 is in state any-error'):
            status.check_agents_started('env1')

    def test_get_agent_versions_1x(self):
        status = Status({
            'machines': {
                '1': {'agent-version': '1.6.2'},
                '2': {'agent-version': '1.6.1'},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/0': {
                            'agent-version': '1.6.1'},
                        'jenkins/1': {},
                    },
                }
            }
        }, '')
        self.assertEqual({
            '1.6.2': {'1'},
            '1.6.1': {'jenkins/0', '2'},
            'unknown': {'jenkins/1'},
        }, status.get_agent_versions())

    def test_get_agent_versions_2x(self):
        status = Status({
            'machines': {
                '1': {'juju-status': {'version': '1.6.2'}},
                '2': {'juju-status': {'version': '1.6.1'}},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/0': {
                            'juju-status': {'version': '1.6.1'}},
                        'jenkins/1': {},
                    },
                }
            }
        }, '')
        self.assertEqual({
            '1.6.2': {'1'},
            '1.6.1': {'jenkins/0', '2'},
            'unknown': {'jenkins/1'},
        }, status.get_agent_versions())

    def test_iter_new_machines(self):
        old_status = Status({
            'machines': {
                'bar': 'bar_info',
            }
        }, '')
        new_status = Status({
            'machines': {
                'foo': 'foo_info',
                'bar': 'bar_info',
            }
        }, '')
        self.assertItemsEqual(new_status.iter_new_machines(old_status),
                              [('foo', 'foo_info')])

    def test_get_instance_id(self):
        status = Status({
            'machines': {
                '0': {'instance-id': 'foo-bar'},
                '1': {},
            }
        }, '')
        self.assertEqual(status.get_instance_id('0'), 'foo-bar')
        with self.assertRaises(KeyError):
            status.get_instance_id('1')
        with self.assertRaises(KeyError):
            status.get_instance_id('2')

    def test_get_machine_dns_name(self):
        status = Status({
            'machines': {
                '0': {'dns-name': '255.1.1.0'},
                '1': {},
            }
        }, '')
        self.assertEqual(status.get_machine_dns_name('0'), '255.1.1.0')
        with self.assertRaisesRegexp(KeyError, 'dns-name'):
            status.get_machine_dns_name('1')
        with self.assertRaisesRegexp(KeyError, '2'):
            status.get_machine_dns_name('2')

    def test_from_text(self):
        text = TestEnvJujuClient.make_status_yaml(
            'agent-state', 'pending', 'horsefeathers')
        status = Status.from_text(text)
        self.assertEqual(status.status_text, text)
        self.assertEqual(status.status, {
            'machines': {'0': {'agent-state': 'pending'}},
            'applications': {'jenkins': {'units': {'jenkins/0': {
                'agent-state': 'horsefeathers'}}}}
        })

    def test_iter_units(self):
        started_unit = {'agent-state': 'started'}
        unit_with_subordinates = {
            'agent-state': 'started',
            'subordinates': {
                'ntp/0': started_unit,
                'nrpe/0': started_unit,
            },
        }
        status = Status({
            'machines': {
                '1': {'agent-state': 'started'},
            },
            'applications': {
                'jenkins': {
                    'units': {
                        'jenkins/0': unit_with_subordinates,
                    }
                },
                'application': {
                    'units': {
                        'application/0': started_unit,
                        'application/1': started_unit,
                    }
                },
            }
        }, '')
        expected = [
            ('application/0', started_unit),
            ('application/1', started_unit),
            ('jenkins/0', unit_with_subordinates),
            ('nrpe/0', started_unit),
            ('ntp/0', started_unit),
        ]
        gen = status.iter_units()
        self.assertIsInstance(gen, types.GeneratorType)
        self.assertEqual(expected, list(gen))

    def test_get_applications_gets_applications(self):
        status = Status({
            'services': {'service': {}},
            'applications': {'application': {}},
            }, '')
        self.assertEqual({'application': {}}, status.get_applications())


class TestServiceStatus(FakeHomeTestCase):

    def test_get_applications_gets_services(self):
        status = ServiceStatus({
            'services': {'service': {}},
            'applications': {'application': {}},
            }, '')
        self.assertEqual({'service': {}}, status.get_applications())


def fast_timeout(count):
    if False:
        yield


@contextmanager
def temp_config():
    with temp_dir() as home:
        os.environ['JUJU_HOME'] = home
        environments_path = os.path.join(home, 'environments.yaml')
        with open(environments_path, 'w') as environments:
            yaml.dump({'environments': {
                'foo': {'type': 'local'}
            }}, environments)
        yield


class TestController(TestCase):

    def test_controller(self):
        controller = Controller('ctrl')
        self.assertEqual('ctrl', controller.name)


class TestSimpleEnvironment(TestCase):

    def test_default_controller(self):
        default = SimpleEnvironment('foo')
        self.assertEqual('foo', default.controller.name)

    def test_clone(self):
        orig = SimpleEnvironment('foo', {'type': 'bar'}, 'myhome')
        orig.local = 'local1'
        orig.kvm = 'kvm1'
        orig.maas = 'maas1'
        orig.joyent = 'joyent1'
        copy = orig.clone()
        self.assertIs(SimpleEnvironment, type(copy))
        self.assertIsNot(orig, copy)
        self.assertEqual(copy.environment, 'foo')
        self.assertIsNot(orig.config, copy.config)
        self.assertEqual({'type': 'bar'}, copy.config)
        self.assertEqual('myhome', copy.juju_home)
        self.assertEqual('local1', copy.local)
        self.assertEqual('kvm1', copy.kvm)
        self.assertEqual('maas1', copy.maas)
        self.assertEqual('joyent1', copy.joyent)
        self.assertIs(orig.controller, copy.controller)

    def test_clone_model_name(self):
        orig = SimpleEnvironment('foo', {'type': 'bar', 'name': 'oldname'},
                                 'myhome')
        copy = orig.clone(model_name='newname')
        self.assertEqual('newname', copy.environment)
        self.assertEqual('newname', copy.config['name'])

    def test_set_model_name(self):
        env = SimpleEnvironment('foo', {})
        env.set_model_name('bar')
        self.assertEqual(env.environment, 'bar')
        self.assertEqual(env.controller.name, 'bar')
        self.assertEqual(env.config['name'], 'bar')

    def test_set_model_name_not_controller(self):
        env = SimpleEnvironment('foo', {})
        env.set_model_name('bar', set_controller=False)
        self.assertEqual(env.environment, 'bar')
        self.assertEqual(env.controller.name, 'foo')
        self.assertEqual(env.config['name'], 'bar')

    def test_local_from_config(self):
        env = SimpleEnvironment('local', {'type': 'openstack'})
        self.assertFalse(env.local, 'Does not respect config type.')
        env = SimpleEnvironment('local', {'type': 'local'})
        self.assertTrue(env.local, 'Does not respect config type.')

    def test_kvm_from_config(self):
        env = SimpleEnvironment('local', {'type': 'local'})
        self.assertFalse(env.kvm, 'Does not respect config type.')
        env = SimpleEnvironment('local',
                                {'type': 'local', 'container': 'kvm'})
        self.assertTrue(env.kvm, 'Does not respect config type.')

    def test_from_config(self):
        with temp_config():
            env = SimpleEnvironment.from_config('foo')
            self.assertIs(SimpleEnvironment, type(env))
            self.assertEqual({'type': 'local'}, env.config)

    def test_from_bogus_config(self):
        with temp_config():
            with self.assertRaises(NoSuchEnvironment):
                SimpleEnvironment.from_config('bar')

    def test_from_config_none(self):
        with temp_config():
            os.environ['JUJU_ENV'] = 'foo'
            # GZ 2015-10-15: Currently default_env calls the juju on path here.
            with patch('jujuconfig.default_env', autospec=True,
                       return_value='foo') as cde_mock:
                env = SimpleEnvironment.from_config(None)
            self.assertEqual(env.environment, 'foo')
            cde_mock.assert_called_once_with()

    def test_juju_home(self):
        env = SimpleEnvironment('foo')
        self.assertIs(None, env.juju_home)
        env = SimpleEnvironment('foo', juju_home='baz')
        self.assertEqual('baz', env.juju_home)

    def test_make_jes_home(self):
        with temp_dir() as juju_home:
            with SimpleEnvironment('foo').make_jes_home(
                    juju_home, 'bar', {'baz': 'qux'}) as jes_home:
                pass
            with open(get_environments_path(jes_home)) as env_file:
                env = yaml.safe_load(env_file)
        self.assertEqual(env, {'baz': 'qux'})
        self.assertEqual(jes_home, jes_home_path(juju_home, 'bar'))

    def test_make_jes_home_clean_existing(self):
        env = SimpleEnvironment('foo')
        with temp_dir() as juju_home:
            with env.make_jes_home(juju_home, 'bar',
                                   {'baz': 'qux'}) as jes_home:
                foo_path = os.path.join(jes_home, 'foo')
                with open(foo_path, 'w') as foo:
                    foo.write('foo')
                self.assertTrue(os.path.isfile(foo_path))
            with env.make_jes_home(juju_home, 'bar',
                                   {'baz': 'qux'}) as jes_home:
                self.assertFalse(os.path.exists(foo_path))

    def test_provider(self):
        env = SimpleEnvironment('foo', {'type': 'provider1'})
        self.assertEqual('provider1', env.provider)

    def test_provider_no_provider(self):
        env = SimpleEnvironment('foo', {'foo': 'bar'})
        with self.assertRaisesRegexp(NoProvider, 'No provider specified.'):
            env.provider

    def test_get_region(self):
        self.assertEqual(
            'bar', SimpleEnvironment(
                'foo', {'type': 'foo', 'region': 'bar'}, 'home').get_region())

    def test_get_region_old_azure(self):
        self.assertEqual('northeu', SimpleEnvironment('foo', {
            'type': 'azure', 'location': 'North EU'}, 'home').get_region())

    def test_get_region_azure_arm(self):
        self.assertEqual('bar', SimpleEnvironment('foo', {
            'type': 'azure', 'location': 'bar', 'tenant-id': 'baz'},
            'home').get_region())

    def test_get_region_joyent(self):
        self.assertEqual('bar', SimpleEnvironment('foo', {
            'type': 'joyent', 'sdc-url': 'https://bar.api.joyentcloud.com'},
            'home').get_region())

    def test_get_region_lxd(self):
        self.assertEqual('localhost', SimpleEnvironment(
            'foo', {'type': 'lxd'}, 'home').get_region())

    def test_get_region_maas(self):
        self.assertIs(None, SimpleEnvironment('foo', {
            'type': 'maas', 'region': 'bar',
        }, 'home').get_region())

    def test_get_region_manual(self):
        self.assertEqual('baz', SimpleEnvironment('foo', {
            'type': 'manual', 'region': 'bar',
            'bootstrap-host': 'baz'}, 'home').get_region())

    def test_set_region(self):
        env = SimpleEnvironment('foo', {'type': 'bar'}, 'home')
        env.set_region('baz')
        self.assertEqual(env.config['region'], 'baz')
        self.assertEqual(env.get_region(), 'baz')

    def test_set_region_no_provider(self):
        env = SimpleEnvironment('foo', {}, 'home')
        env.set_region('baz')
        self.assertEqual(env.config['region'], 'baz')

    def test_set_region_joyent(self):
        env = SimpleEnvironment('foo', {'type': 'joyent'}, 'home')
        env.set_region('baz')
        self.assertEqual(env.config['sdc-url'],
                         'https://baz.api.joyentcloud.com')
        self.assertEqual(env.get_region(), 'baz')

    def test_set_region_azure(self):
        env = SimpleEnvironment('foo', {'type': 'azure'}, 'home')
        env.set_region('baz')
        self.assertEqual(env.config['location'], 'baz')
        self.assertEqual(env.get_region(), 'baz')

    def test_set_region_lxd(self):
        env = SimpleEnvironment('foo', {'type': 'lxd'}, 'home')
        with self.assertRaisesRegexp(ValueError,
                                     'Only "localhost" allowed for lxd.'):
            env.set_region('baz')
        env.set_region('localhost')
        self.assertEqual(env.get_region(), 'localhost')

    def test_set_region_manual(self):
        env = SimpleEnvironment('foo', {'type': 'manual'}, 'home')
        env.set_region('baz')
        self.assertEqual(env.config['bootstrap-host'], 'baz')
        self.assertEqual(env.get_region(), 'baz')

    def test_set_region_maas(self):
        env = SimpleEnvironment('foo', {'type': 'maas'}, 'home')
        with self.assertRaisesRegexp(ValueError,
                                     'Only None allowed for maas.'):
            env.set_region('baz')
        env.set_region(None)
        self.assertIs(env.get_region(), None)

    def test_get_cloud_credentials_returns_config(self):
        env = SimpleEnvironment(
            'foo', {'type': 'ec2', 'region': 'foo'}, 'home')
        env.credentials = {'credentials': {
            'aws': {'credentials': {'aws': True}},
            'azure': {'credentials': {'azure': True}},
            }}
        self.assertEqual(env.config, env.get_cloud_credentials())

    def test_dump_yaml(self):
        env = SimpleEnvironment('baz', {'type': 'qux'}, 'home')
        with temp_dir() as path:
            env.dump_yaml(path, {'foo': 'bar'})
            self.assertItemsEqual(
                ['environments.yaml'], os.listdir(path))
            with open(os.path.join(path, 'environments.yaml')) as f:
                self.assertEqual({'foo': 'bar'}, yaml.safe_load(f))


class TestJujuData(TestCase):

    def test_clone(self):
        orig = JujuData('foo', {'type': 'bar'}, 'myhome')
        orig.credentials = {'secret': 'password'}
        orig.clouds = {'name': {'meta': 'data'}}
        copy = orig.clone()
        self.assertIs(JujuData, type(copy))
        self.assertIsNot(orig, copy)
        self.assertEqual(copy.environment, 'foo')
        self.assertIsNot(orig.config, copy.config)
        self.assertEqual({'type': 'bar'}, copy.config)
        self.assertEqual('myhome', copy.juju_home)
        self.assertIsNot(orig.credentials, copy.credentials)
        self.assertEqual(orig.credentials, copy.credentials)
        self.assertIsNot(orig.clouds, copy.clouds)
        self.assertEqual(orig.clouds, copy.clouds)

    def test_clone_model_name(self):
        orig = JujuData('foo', {'type': 'bar', 'name': 'oldname'}, 'myhome')
        orig.credentials = {'secret': 'password'}
        orig.clouds = {'name': {'meta': 'data'}}
        copy = orig.clone(model_name='newname')
        self.assertEqual('newname', copy.environment)
        self.assertEqual('newname', copy.config['name'])

    def test_get_cloud_random_provider(self):
        self.assertEqual(
            'bar', JujuData('foo', {'type': 'bar'}, 'home').get_cloud())

    def test_get_cloud_ec2(self):
        self.assertEqual(
            'aws', JujuData('foo', {'type': 'ec2', 'region': 'bar'},
                            'home').get_cloud())
        self.assertEqual(
            'aws-china', JujuData('foo', {
                'type': 'ec2', 'region': 'cn-north-1'
                }, 'home').get_cloud())

    def test_get_cloud_gce(self):
        self.assertEqual(
            'google', JujuData('foo', {'type': 'gce', 'region': 'bar'},
                               'home').get_cloud())

    def test_get_cloud_maas(self):
        data = JujuData('foo', {'type': 'maas', 'maas-server': 'bar'}, 'home')
        data.clouds = {'clouds': {
            'baz': {'type': 'maas', 'endpoint': 'bar'},
            'qux': {'type': 'maas', 'endpoint': 'qux'},
            }}
        self.assertEqual('baz', data.get_cloud())

    def test_get_cloud_maas_wrong_type(self):
        data = JujuData('foo', {'type': 'maas', 'maas-server': 'bar'}, 'home')
        data.clouds = {'clouds': {
            'baz': {'type': 'foo', 'endpoint': 'bar'},
            }}
        with self.assertRaisesRegexp(LookupError, 'No such endpoint: bar'):
            self.assertEqual(data.get_cloud())

    def test_get_cloud_openstack(self):
        data = JujuData('foo', {'type': 'openstack', 'auth-url': 'bar'},
                        'home')
        data.clouds = {'clouds': {
            'baz': {'type': 'openstack', 'endpoint': 'bar'},
            'qux': {'type': 'openstack', 'endpoint': 'qux'},
            }}
        self.assertEqual('baz', data.get_cloud())

    def test_get_cloud_openstack_wrong_type(self):
        data = JujuData('foo', {'type': 'openstack', 'auth-url': 'bar'},
                        'home')
        data.clouds = {'clouds': {
            'baz': {'type': 'maas', 'endpoint': 'bar'},
            }}
        with self.assertRaisesRegexp(LookupError, 'No such endpoint: bar'):
            data.get_cloud()

    def test_get_cloud_credentials(self):
        juju_data = JujuData('foo', {'type': 'ec2', 'region': 'foo'}, 'home')
        juju_data.credentials = {'credentials': {
            'aws': {'credentials': {'aws': True}},
            'azure': {'credentials': {'azure': True}},
            }}
        self.assertEqual({'aws': True}, juju_data.get_cloud_credentials())

    def test_dump_yaml(self):
        cloud_dict = {'clouds': {'foo': {}}}
        credential_dict = {'credential': {'bar': {}}}
        data = JujuData('baz', {'type': 'qux'}, 'home')
        data.clouds = dict(cloud_dict)
        data.credentials = dict(credential_dict)
        with temp_dir() as path:
            data.dump_yaml(path, {})
            self.assertItemsEqual(
                ['clouds.yaml', 'credentials.yaml'], os.listdir(path))
            with open(os.path.join(path, 'clouds.yaml')) as f:
                self.assertEqual(cloud_dict, yaml.safe_load(f))
            with open(os.path.join(path, 'credentials.yaml')) as f:
                self.assertEqual(credential_dict, yaml.safe_load(f))

    def test_load_yaml(self):
        cloud_dict = {'clouds': {'foo': {}}}
        credential_dict = {'credential': {'bar': {}}}
        with temp_dir() as path:
            with open(os.path.join(path, 'clouds.yaml'), 'w') as f:
                yaml.safe_dump(cloud_dict, f)
            with open(os.path.join(path, 'credentials.yaml'), 'w') as f:
                yaml.safe_dump(credential_dict, f)
            data = JujuData('baz', {'type': 'qux'}, path)
            data.load_yaml()


class TestGroupReporter(TestCase):

    def test_single(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        self.assertEqual(sio.getvalue(), "")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1")
        reporter.update({"done": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1\n")

    def test_single_ticks(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1 .")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1 ..")
        reporter.update({"done": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1 ..\n")

    def test_multiple_values(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.update({"working": ["1", "2"]})
        self.assertEqual(sio.getvalue(), "working: 1, 2")
        reporter.update({"working": ["1"], "done": ["2"]})
        self.assertEqual(sio.getvalue(), "working: 1, 2\nworking: 1")
        reporter.update({"done": ["1", "2"]})
        self.assertEqual(sio.getvalue(), "working: 1, 2\nworking: 1\n")

    def test_multiple_groups(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.update({"working": ["1", "2"], "starting": ["3"]})
        first = "starting: 3 | working: 1, 2"
        self.assertEqual(sio.getvalue(), first)
        reporter.update({"working": ["1", "3"], "done": ["2"]})
        second = "working: 1, 3"
        self.assertEqual(sio.getvalue(), "\n".join([first, second]))
        reporter.update({"done": ["1", "2", "3"]})
        self.assertEqual(sio.getvalue(), "\n".join([first, second, ""]))

    def test_finish(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        self.assertEqual(sio.getvalue(), "")
        reporter.update({"working": ["1"]})
        self.assertEqual(sio.getvalue(), "working: 1")
        reporter.finish()
        self.assertEqual(sio.getvalue(), "working: 1\n")

    def test_finish_unchanged(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        self.assertEqual(sio.getvalue(), "")
        reporter.finish()
        self.assertEqual(sio.getvalue(), "")

    def test_wrap_to_width(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        self.assertEqual(sio.getvalue(), "")
        for _ in range(150):
            reporter.update({"working": ["1"]})
        reporter.finish()
        self.assertEqual(sio.getvalue(), """\
working: 1 ....................................................................
...............................................................................
..
""")

    def test_wrap_to_width_exact(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.wrap_width = 12
        self.assertEqual(sio.getvalue(), "")
        changes = []
        for _ in range(20):
            reporter.update({"working": ["1"]})
            changes.append(sio.getvalue())
        self.assertEqual(changes[::4], [
            "working: 1",
            "working: 1 .\n...",
            "working: 1 .\n.......",
            "working: 1 .\n...........",
            "working: 1 .\n............\n...",
        ])
        reporter.finish()
        self.assertEqual(sio.getvalue(), changes[-1] + "\n")

    def test_wrap_to_width_overflow(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.wrap_width = 8
        self.assertEqual(sio.getvalue(), "")
        changes = []
        for _ in range(16):
            reporter.update({"working": ["1"]})
            changes.append(sio.getvalue())
        self.assertEqual(changes[::4], [
            "working: 1",
            "working: 1\n....",
            "working: 1\n........",
            "working: 1\n........\n....",
        ])
        reporter.finish()
        self.assertEqual(sio.getvalue(), changes[-1] + "\n")

    def test_wrap_to_width_multiple_groups(self):
        sio = StringIO.StringIO()
        reporter = GroupReporter(sio, "done")
        reporter.wrap_width = 16
        self.assertEqual(sio.getvalue(), "")
        changes = []
        for _ in range(6):
            reporter.update({"working": ["1", "2"]})
            changes.append(sio.getvalue())
        for _ in range(10):
            reporter.update({"working": ["1"], "done": ["2"]})
            changes.append(sio.getvalue())
        self.assertEqual(changes[::4], [
            "working: 1, 2",
            "working: 1, 2 ..\n..",
            "working: 1, 2 ..\n...\n"
            "working: 1 ..",
            "working: 1, 2 ..\n...\n"
            "working: 1 .....\n.",
        ])
        reporter.finish()
        self.assertEqual(sio.getvalue(), changes[-1] + "\n")


class AssessParseStateServerFromErrorTestCase(TestCase):

    def test_parse_new_state_server_from_error(self):
        output = dedent("""
            Waiting for address
            Attempting to connect to 10.0.0.202:22
            Attempting to connect to 1.2.3.4:22
            The fingerprint for the ECDSA key sent by the remote host is
            """)
        error = subprocess.CalledProcessError(1, ['foo'], output)
        address = parse_new_state_server_from_error(error)
        self.assertEqual('1.2.3.4', address)

    def test_parse_new_state_server_from_error_output_none(self):
        error = subprocess.CalledProcessError(1, ['foo'], None)
        address = parse_new_state_server_from_error(error)
        self.assertIs(None, address)

    def test_parse_new_state_server_from_error_no_output(self):
        address = parse_new_state_server_from_error(Exception())
        self.assertIs(None, address)


class TestGetMachineDNSName(TestCase):

    log_level = logging.DEBUG

    machine_0_no_addr = """\
        machines:
            "0":
                instance-id: pending
        """

    machine_0_hostname = """\
        machines:
            "0":
                dns-name: a-host
        """

    machine_0_ipv6 = """\
        machines:
            "0":
                dns-name: 2001:db8::3
        """

    def test_gets_host(self):
        status = Status.from_text(self.machine_0_hostname)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status]
        host = get_machine_dns_name(fake_client, '0')
        self.assertEqual(host, "a-host")
        fake_client.status_until.assert_called_once_with(timeout=600)
        self.assertEqual(self.log_stream.getvalue(), "")

    def test_retries_for_dns_name(self):
        status_pending = Status.from_text(self.machine_0_no_addr)
        status_host = Status.from_text(self.machine_0_hostname)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status_pending, status_host]
        host = get_machine_dns_name(fake_client, '0')
        self.assertEqual(host, "a-host")
        fake_client.status_until.assert_called_once_with(timeout=600)
        self.assertEqual(
            self.log_stream.getvalue(),
            "DEBUG No dns-name yet for machine 0\n")

    def test_retries_gives_up(self):
        status = Status.from_text(self.machine_0_no_addr)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status] * 3
        host = get_machine_dns_name(fake_client, '0', timeout=10)
        self.assertEqual(host, None)
        fake_client.status_until.assert_called_once_with(timeout=10)
        self.assertEqual(
            self.log_stream.getvalue(),
            "DEBUG No dns-name yet for machine 0\n" * 3)

    def test_gets_ipv6(self):
        status = Status.from_text(self.machine_0_ipv6)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status]
        host = get_machine_dns_name(fake_client, '0')
        self.assertEqual(host, "2001:db8::3")
        fake_client.status_until.assert_called_once_with(timeout=600)
        self.assertEqual(
            self.log_stream.getvalue(),
            "WARNING Selected IPv6 address for machine 0: '2001:db8::3'\n")

    def test_gets_ipv6_unsupported(self):
        status = Status.from_text(self.machine_0_ipv6)
        fake_client = Mock(spec=['status_until'])
        fake_client.status_until.return_value = [status]
        with patch('utility.socket', wraps=socket) as wrapped_socket:
            del wrapped_socket.inet_pton
            host = get_machine_dns_name(fake_client, '0')
        self.assertEqual(host, "2001:db8::3")
        fake_client.status_until.assert_called_once_with(timeout=600)
        self.assertEqual(self.log_stream.getvalue(), "")

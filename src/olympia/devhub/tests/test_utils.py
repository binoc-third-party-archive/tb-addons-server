# -*- coding: utf-8 -*-
import os.path

from django.conf import settings
from django.test.utils import override_settings

import mock
from waffle.testutils import override_switch

from celery.result import AsyncResult
from six import text_type

from olympia import amo
from olympia.amo.tests import (
    addon_factory, TestCase, user_factory, version_factory)
from olympia.devhub import tasks, utils
from olympia.files.models import FileUpload
from olympia.lib.akismet.models import AkismetReport


class TestValidatorBase(TestCase):

    def setUp(self):
        # Create File objects for version 1.0 and 1.1.
        self.addon = addon_factory(
            guid='test-desktop@nowhere', slug='test-amo-addon',
            version_kw={'version': '1.0'})
        self.version = self.addon.current_version
        self.file = self.version.files.get()
        self.version_1_1 = version_factory(addon=self.addon, version='1.1')
        self.file_1_1 = self.version_1_1.files.get()

        # Creating the files and versions above resets this.
        self.addon.update(status=amo.STATUS_PUBLIC)

        # Create a FileUpload object for an XPI containing version 1.1.
        path = os.path.join(settings.ROOT,
                            'src/olympia/devhub/tests/addons/desktop.xpi')
        self.file_upload = FileUpload.objects.create(path=path)
        self.xpi_version = '1.1'

        # Patch validation tasks that we expect the validator to call.
        self.patchers = []
        self.save_file = self.patch(
            'olympia.devhub.tasks.handle_file_validation_result').subtask
        self.save_upload = self.patch(
            'olympia.devhub.tasks.handle_upload_validation_result').subtask

        self.validate_file = self.patch(
            'olympia.devhub.tasks.validate_file').subtask
        self.validate_upload = self.patch(
            'olympia.devhub.tasks.validate_file_path').subtask

    def patch(self, thing):
        """Patch the given "thing", and revert the patch on test teardown."""
        patcher = mock.patch(thing)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def check_upload(self, file_upload, listed=True):
        """Check that the given new file upload is validated properly."""
        # Run validator.
        utils.Validator(file_upload, listed=listed)

        # We shouldn't be attempting to validate an existing file.
        assert not self.validate_file.called

        # Make sure we run the correct validation task for the upload.
        self.validate_upload.assert_called_once_with(
            [file_upload.path],
            {'hash_': file_upload.hash, 'listed': listed,
             'is_webextension': False})

        # Make sure we run the correct save validation task, with a
        # fallback error handler.
        channel = (amo.RELEASE_CHANNEL_LISTED if listed
                   else amo.RELEASE_CHANNEL_UNLISTED)
        self.save_upload.assert_has_calls([
            mock.call([mock.ANY, file_upload.pk, channel, False],
                      immutable=True),
            mock.call([file_upload.pk, channel, False], link_error=mock.ANY)])

    def check_file(self, file_):
        """Check that the given file is validated properly."""
        # Run validator.
        utils.Validator(file_)

        # We shouldn't be attempting to validate a bare upload.
        assert not self.validate_upload.called

        # Make sure we run the correct validation task.
        self.validate_file.assert_called_once_with(
            [file_.pk],
            {'hash_': file_.original_hash, 'is_webextension': False})

        # Make sure we run the correct save validation task, with a
        # fallback error handler.
        self.save_file.assert_has_calls([
            mock.call([mock.ANY, file_.pk, file_.version.channel, False],
                      immutable=True),
            mock.call([file_.pk, file_.version.channel, False],
                      link_error=mock.ANY)])


class TestValidatorListed(TestValidatorBase):
    @mock.patch('celery.Signature.__or__')
    def test_run_once_per_file(self, chain_mock):
        """Tests that only a single validation task is run for a given file."""
        task = mock.Mock()
        chain_mock.return_value = task
        task.delay.return_value = mock.Mock(task_id='42')

        assert isinstance(tasks.validate(self.file), mock.Mock)
        assert task.delay.call_count == 1

        assert isinstance(tasks.validate(self.file), AsyncResult)
        assert task.delay.call_count == 1

        assert isinstance(tasks.validate(self.file_1_1), mock.Mock)
        assert task.delay.call_count == 2

    @mock.patch('celery.Signature.__or__')
    def test_run_once_file_upload(self, chain_mock):
        """Tests that only a single validation task is run for a given file
        upload."""
        task = mock.Mock()
        chain_mock.return_value = task
        task.delay.return_value = mock.Mock(task_id='42')

        assert isinstance(
            tasks.validate(self.file_upload, listed=True), mock.Mock)
        assert task.delay.call_count == 1

        assert isinstance(
            tasks.validate(self.file_upload, listed=True), AsyncResult)
        assert task.delay.call_count == 1

    def test_cache_key(self):
        """Tests that the correct cache key is generated for a given object."""

        assert (utils.Validator(self.file).cache_key ==
                'validation-task:files.File:{0}:None'.format(self.file.pk))

        assert (utils.Validator(self.file_upload, listed=False).cache_key ==
                'validation-task:files.FileUpload:{0}:False'.format(
                    self.file_upload.pk))

    @mock.patch('olympia.devhub.utils.parse_addon')
    def test_search_plugin(self, parse_addon):
        """Test that search plugins are handled correctly."""

        parse_addon.return_value = {
            'guid': None,
            'version': '20140103',
            'is_webextension': False,
        }

        addon = addon_factory(type=amo.ADDON_SEARCH,
                              version_kw={'version': '20140101'})

        assert addon.guid is None
        self.check_upload(self.file_upload)

        self.validate_upload.reset_mock()
        self.save_file.reset_mock()

        version = version_factory(addon=addon, version='20140102')
        self.check_file(version.files.get())


class TestLimitValidationResults(TestCase):
    """Test that higher priority messages are truncated last."""

    def make_validation(self, types):
        """Take a list of error types and make a
        validation results dict."""
        validation = {
            'messages': [],
            'errors': 0,
            'warnings': 0,
            'notices': 0,
        }
        severities = ['low', 'medium', 'high']
        for type_ in types:
            if type_ in severities:
                type_ = 'warning'
            validation[type_ + 's'] += 1
            validation['messages'].append({'type': type_})
        return validation

    @override_settings(VALIDATOR_MESSAGE_LIMIT=2)
    def test_errors_are_first(self):
        validation = self.make_validation(
            ['error', 'warning', 'notice', 'error'])
        utils.limit_validation_results(validation)
        limited = validation['messages']
        assert len(limited) == 3
        assert '2 messages were truncated' in limited[0]['message']
        assert limited[1]['type'] == 'error'
        assert limited[2]['type'] == 'error'


class TestFixAddonsLinterOutput(TestCase):

    def test_fix_output(self):
        original_output = {
            'count': 4,
            'summary': {
                'errors': 0,
                'notices': 0,
                'warnings': 4
            },
            'metadata': {
                'manifestVersion': 2,
                'name': 'My Dogs New Tab',
                'type': 1,
                'version': '2.13.15',
                'architecture': 'extension',
                'emptyFiles': [],
                'jsLibs': {
                    'lib/vendor/jquery.js': 'jquery.2.1.4.jquery.js'
                }
            },
            'errors': [],
            'notices': [],
            'warnings': [
                {
                    '_type': 'warning',
                    'code': 'MANIFEST_PERMISSIONS',
                    'message': '/permissions: Unknown permissions ...',
                    'description': 'See https://mzl.la/1R1n1t0 ...',
                    'file': 'manifest.json'
                },
                {
                    '_type': 'warning',
                    'code': 'MANIFEST_PERMISSIONS',
                    'message': '/permissions: Unknown permissions ...',
                    'description': 'See https://mzl.la/1R1n1t0 ....',
                    'file': 'manifest.json'
                },
                {
                    '_type': 'warning',
                    'code': 'MANIFEST_CSP',
                    'message': '\'content_security_policy\' is ...',
                    'description': 'A custom content_security_policy ...'
                },
                {
                    '_type': 'warning',
                    'code': 'NO_DOCUMENT_WRITE',
                    'message': 'Use of document.write strongly discouraged.',
                    'description': 'document.write will fail in...',
                    'column': 13,
                    'file': 'lib/vendor/knockout.js',
                    'line': 5449
                }
            ]
        }

        fixed = utils.fix_addons_linter_output(original_output)

        assert fixed['success']
        assert fixed['warnings'] == 4
        assert 'uid' in fixed['messages'][0]
        assert 'id' in fixed['messages'][0]
        assert 'type' in fixed['messages'][0]
        assert fixed['messages'][0]['tier'] == 1
        assert fixed['compatibility_summary'] == {
            'warnings': 0,
            'errors': 0,
            'notices': 0,
        }
        assert fixed['ending_tier'] == 5
        assert fixed['metadata']['is_webextension'] is True
        assert fixed['metadata']['processed_by_addons_linter'] is True
        assert fixed['metadata']['listed'] is True
        assert fixed['metadata']['identified_files'] == {
            'lib/vendor/jquery.js': {'path': 'jquery.2.1.4.jquery.js'}
        }
        # Make sure original metadata was preserved.
        for key, value in original_output['metadata'].items():
            assert fixed['metadata'][key] == value


@override_switch('akismet-spam-check', active=True)
class TestGetAddonAkismetReports(TestCase):
    def setUp(self):
        super(TestGetAddonAkismetReports, self).setUp()

        patcher = mock.patch.object(
            AkismetReport, 'create_for_addon')
        self.addCleanup(patcher.stop)
        self.create_for_addon_mock = patcher.start()
        self.parse_addon_mock = self.patch('olympia.devhub.utils.parse_addon')

    @override_switch('akismet-spam-check', active=False)
    def test_waffle_off(self):
        reports = utils.get_addon_akismet_reports(
            None, '', '')
        assert reports == []
        self.create_for_addon_mock.assert_not_called()

    def test_upload(self):
        user = user_factory()
        upload = FileUpload.objects.create()
        self.parse_addon_mock.return_value = {'description': u'f????'}
        user_agent = 'Mr User/Agent'
        referrer = 'http://foo.baa/'
        reports = utils.get_addon_akismet_reports(
            user, user_agent, referrer, upload=upload)
        assert len(reports) == 1
        self.create_for_addon_mock.assert_called_with(
            upload=upload, addon=None, user=user, property_name='description',
            property_value=u'f????', user_agent=user_agent, referrer=referrer)
        self.create_for_addon_mock.assert_called_once()

    def test_upload_with_addon(self):
        # Give addon some existing metadata.
        addon = addon_factory()
        user = user_factory()
        upload = FileUpload.objects.create(addon=addon)
        # summary is parsed but it's in existing_data - i.e. should have
        # been spam checked previous it so will be ignored.
        self.parse_addon_mock.return_value = {
            'name': u'f????', 'summary': u'summ??ry'}
        user_agent = 'Mr User/Agent'
        referrer = 'http://foo.baa/'
        reports = utils.get_addon_akismet_reports(
            user, user_agent, referrer, upload=upload,
            existing_data=[u'summ??ry'])
        # only one, no summary because it's in existing data
        assert len(reports) == 1
        self.create_for_addon_mock.assert_called_with(
            upload=upload, addon=addon, user=user, property_name='name',
            property_value=u'f????', user_agent=user_agent, referrer=referrer)
        self.create_for_addon_mock.assert_called_once()

    def test_upload_locales(self):
        addon = addon_factory(summary=u'??Och??!', default_locale='es-AR')
        user = user_factory()
        upload = FileUpload.objects.create(addon=addon)
        existing_data = utils.fetch_existing_translations_from_addon(
            addon, ('summary', 'name', 'description'))
        # check fetch_existing_translations_from_addon worked okay
        assert existing_data == {text_type(addon.name), u'??Och??!'}
        self.parse_addon_mock.return_value = {
            'description': {
                'en-US': u'f????',
                'fr': u'l?? foo',
                'de': '',  # should be ignored because empty
                'es-ES': u'??Och??!'  # ignored because in existing_data
            },
            'name': u'just one name',
            'summary': None,  # should also be ignored because None
        }
        user_agent = 'Mr User/Agent'
        referrer = 'http://foo.baa/'
        reports = utils.get_addon_akismet_reports(
            user, user_agent, referrer, upload=upload,
            existing_data=existing_data)
        assert len(reports) == 3
        assert self.create_for_addon_mock.call_count == 3
        calls = [
            mock.call(
                upload=upload, addon=addon, user=user,
                property_name='name', property_value=u'just one name',
                user_agent=user_agent, referrer=referrer),
            mock.call(
                upload=upload, addon=addon, user=user,
                property_name='description', property_value=u'f????',
                user_agent=user_agent, referrer=referrer),
            mock.call(
                upload=upload, addon=addon, user=user,
                property_name='description', property_value=u'l?? foo',
                user_agent=user_agent, referrer=referrer)]
        self.create_for_addon_mock.assert_has_calls(calls, any_order=True)

    def test_addon_update(self):
        addon = addon_factory(summary=u'??Och??!', default_locale='es-AR')
        user = user_factory()
        existing_data = utils.fetch_existing_translations_from_addon(
            addon, ('summary', 'name', 'description'))
        # check fetch_existing_translations_from_addon worked okay
        assert existing_data == {text_type(addon.name), u'??Och??!'}
        cleaned_data = {
            'description': {
                'en-US': u'f????',
                'fr': u'l?? foo',
                'de': '',  # should be ignored because empty
                'es-ES': u'??Och??!'  # ignored because in exist_data
            },
            'name': {
                'en-GB': u'just one name',
                'fr': None,
            },
        }
        user_agent = 'Mr User/Agent'
        referrer = 'http://foo.baa/'
        reports = utils.get_addon_akismet_reports(
            user, user_agent, referrer, addon=addon, data=cleaned_data,
            existing_data=existing_data)
        assert len(reports) == 3
        assert self.create_for_addon_mock.call_count == 3
        calls = [
            mock.call(
                upload=None, addon=addon, user=user,
                property_name='name', property_value=u'just one name',
                user_agent=user_agent, referrer=referrer),
            mock.call(
                upload=None, addon=addon, user=user,
                property_name='description', property_value=u'f????',
                user_agent=user_agent, referrer=referrer),
            mock.call(
                upload=None, addon=addon, user=user,
                property_name='description', property_value=u'l?? foo',
                user_agent=user_agent, referrer=referrer)]
        self.create_for_addon_mock.assert_has_calls(calls, any_order=True)

# coding: utf-8
""" Common test utilities. """
import os
import sys
import tempfile
from contextlib import suppress
from logging import getLogger
from pathlib import Path
from threading import Thread
from time import sleep
from typing import Tuple
from unittest import TestCase
from uuid import uuid4

from faker import Faker
from PyQt5.QtCore import QCoreApplication, QTimer, pyqtSignal, pyqtSlot
from nuxeo.models import Document, User

from sentry_sdk import configure_scope

from nxdrive import __version__
from nxdrive.constants import LINUX, MAC, WINDOWS
from nxdrive.engine.watcher.local_watcher import WIN_MOVE_RESOLUTION_PERIOD
from nxdrive.manager import Manager
from nxdrive.options import Options
from nxdrive.translator import Translator
from nxdrive.utils import normalized_path
from . import DocRemote, LocalTest, RemoteBase, RemoteTest
from ..utils import clean_dir, salt

# Default remote watcher delay used for tests
TEST_DEFAULT_DELAY = 3

TEST_WS_DIR = "/default-domain/workspaces"
FS_ITEM_ID_PREFIX = "defaultFileSystemItemFactory#default#"
SYNC_ROOT_FAC_ID = "defaultSyncRootFolderItemFactory#default#"

# 1s time resolution as we truncate remote last modification time to the
# seconds in RemoteFileInfo.from_dict() because of the datetime
# resolution of some databases (MySQL...)
REMOTE_MODIFICATION_TIME_RESOLUTION = 1.0

# 1s resolution on HFS+ on OSX
# ~0.01  sec for NTFS
#  0.001 sec for EXT4FS
OS_STAT_MTIME_RESOLUTION = 1.0

log = getLogger(__name__)

DEFAULT_NUXEO_URL = "http://localhost:8080/nuxeo"
DEFAULT_WAIT_SYNC_TIMEOUT = 10
FILE_CONTENT = b"Lorem ipsum dolor sit amet ..."
FAKER = Faker("en_US")
LOCATION = normalized_path(__file__).parent.parent


Translator(LOCATION / "resources" / "i18n")


def nuxeo_url() -> str:
    """Retrieve the Nuxeo URL."""
    url = os.getenv("NXDRIVE_TEST_NUXEO_URL", DEFAULT_NUXEO_URL)
    url = url.split("#")[0]
    return url


def root_remote(base_folder: str = "/") -> DocRemote:
    return DocRemote(
        nuxeo_url(),
        "Administrator",
        "nxdrive-test-administrator-device",
        __version__,
        password="Administrator",
        base_folder=base_folder,
        timeout=60,
    )


class StubQApplication(QCoreApplication):
    bindEngine = pyqtSignal(int, bool)
    unbindEngine = pyqtSignal(int)

    def __init__(self, argv, test_case):
        super().__init__(argv)
        self._test = test_case
        self.bindEngine.connect(self.bind_engine)
        self.unbindEngine.connect(self.unbind_engine)

    @pyqtSlot(int, bool)
    def bind_engine(self, number, start_engine):
        self._test.bind_engine(number, start_engine=start_engine)

    @pyqtSlot(int)
    def unbind_engine(self, number):
        self._test.unbind_engine(number)


class TwoUsersTest(TestCase):
    def setup_method(
        self, test_method, register_roots=True, user_2=True, server_profile=None
    ):
        """ Setup method that will be invoked for every test method of a class."""

        log.info("TEST master setup start")

        self.current_test = test_method.__name__

        # To be replaced with fixtures when migrating to 100% pytest
        self.nuxeo_url = nuxeo_url()  # fixture name: nuxeo_url
        self.version = __version__  # fixture name: version
        self.root_remote = root_remote()
        self.fake = FAKER
        self.location = LOCATION

        self.server_profile = server_profile
        if server_profile:
            self.root_remote.activate_profile(server_profile)

        self.users = [self._create_user(1)]
        if user_2:
            self.users.append(self._create_user(2))
        self._create_workspace(self.current_test)

        # Add proper rights for all users on the root workspace
        users = [user.uid for user in self.users]
        self.ws.add_permission({"permission": "ReadWrite", "users": users})

        Options.delay = TEST_DEFAULT_DELAY
        self.connected = False

        self.app = StubQApplication([], self)

        self.report_path = os.getenv("REPORT_PATH")

        self.tmpdir = (
            normalized_path(tempfile.gettempdir()) / str(uuid4()).split("-")[0]
        )
        self.upload_tmp_dir = self.tmpdir / "uploads"
        self.upload_tmp_dir.mkdir(parents=True)

        self._append_user_attrs(1, register_roots)
        if user_2:
            self._append_user_attrs(2, register_roots)

    def teardown_method(self, test_method):
        """ Clean-up method. """

        log.info("TEST master teardown start")

        self.generate_report()

        if self.server_profile:
            self.root_remote.deactivate_profile(self.server_profile)

        for user in list(self.users):
            self.root_remote.users.delete(user.username)
            self.users.remove(user)

        self.ws.delete()

        self.manager_1.close()
        clean_dir(self.local_test_folder_1)
        clean_dir(self.upload_tmp_dir)

        if hasattr(self, "manager_2"):
            self.manager_2.close()
            clean_dir(self.local_test_folder_2)

        clean_dir(self.tmpdir)

        log.info("TEST master teardown end")

    def run(self, result=None):
        """
        I could not (yet?) migrate this method to pytest because I did not
        find a way to make tests pass.
        We need to start each test in a thread and call self.app.exec_() to
        let signals transit in the QApplication.
        """

        log.info("TEST run start")

        def launch_test():
            # Note: we cannot use super().run(result) here
            super(TwoUsersTest, self).run(result)

            with suppress(Exception):
                self.app.quit()

        # Ensure to kill the app if it is taking too long.
        # We need to do that because sometimes a thread get blocked and so the test suite.
        # Here, we set the timeout to 00:02:00, let's see if a higher value is needed.
        timeout = 2 * 60

        def kill_test():
            log.error(f"Killing {self.id()} after {timeout} seconds")
            self.app.quit()

        QTimer.singleShot(timeout * 1000, kill_test)

        # Start the app and let signals transit between threads!
        sync_thread = Thread(target=launch_test)

        with configure_scope() as scope:
            scope.set_tag("test", self.current_test)
            sync_thread.start()
            self.app.exec_()
            sync_thread.join(30)

        log.info("TEST run end")

    def _create_user(self, number: int) -> User:
        def _company_domain(company_: str) -> str:
            company_domain = company_.lower()
            company_domain = company_domain.replace(",", "_")
            company_domain = company_domain.replace(" ", "_")
            company_domain = company_domain.replace("-", "_")
            company_domain = company_domain.replace("__", "_")

        company = self.fake.company()
        company_domain = _company_domain(company)
        first_name, last_name = self.fake.name().split(" ", 1)
        username = salt(first_name.lower())
        properties = {
            "lastName": last_name,
            "firstName": first_name,
            "email": f"{username}@{company_domain}.org",
            "password": username,
            "username": username,
        }

        user = self.root_remote.users.create(User(properties=properties))

        # Convenient attributes
        for k, v in properties.items():
            setattr(user, k, v)

        setattr(self, f"user_{number}", username)
        setattr(self, f"password_{number}", username)

        return user

    def _create_workspace(self, title: str) -> Document:
        title = salt(title, prefix="")
        new_ws = Document(name=title, type="Workspace", properties={"dc:title": title})
        self.ws = self.root_remote.documents.create(new_ws, parent_path=TEST_WS_DIR)
        self.workspace = self.ws.uid
        self.workspace_title = self.ws.title
        return self.ws

    def _append_user_attrs(self, number: int, register_roots: bool) -> None:
        """Create all stuff needed for one user. Ugly but usefull."""

        # Create all what we need
        local_test_folder = self.tmpdir / str(number)
        local_nxdrive_folder = local_test_folder / "drive"
        local_nxdrive_folder.mkdir(parents=True)
        nxdrive_conf_folder = local_test_folder / "conf"
        nxdrive_conf_folder.mkdir()
        manager = Manager(nxdrive_conf_folder)
        user = getattr(self, f"user_{number}")
        password = getattr(self, f"password_{number}")
        engine = self.bind_engine(
            number,
            start_engine=False,
            manager=manager,
            user=user,
            password=password,
            folder=local_nxdrive_folder,
        )
        queue_manager = engine.get_queue_manager()
        sync_root_folder = local_nxdrive_folder / self.workspace_title
        local_root_client = self.get_local_client(engine.local.base_folder)
        local = self.get_local_client(sync_root_folder)
        remote_document_client = DocRemote(
            self.nuxeo_url,
            getattr(self, f"user_{number}"),
            f"nxdrive-test-device-{number}",
            self.version,
            password=getattr(self, f"password_{number}"),
            base_folder=self.workspace,
            upload_tmp_dir=self.upload_tmp_dir,
        )
        remote = RemoteBase(
            self.nuxeo_url,
            getattr(self, f"user_{number}"),
            f"nxdrive-test-device-{number}",
            self.version,
            password=getattr(self, f"password_{number}"),
            base_folder=self.workspace,
            upload_tmp_dir=self.upload_tmp_dir,
            dao=engine._dao,
        )
        if register_roots:
            remote.register_as_root(self.workspace)

        # Force deletion behavior to real deletion for all tests
        manager._dao.update_config("deletion_behavior", "delete_server")
        manager._dao.store_bool("show_deletion_prompt", False)

        # And now persist in attributes
        setattr(self, f"manager_{number}", manager)
        setattr(self, f"local_test_folder_{number}", local_test_folder)
        setattr(self, f"local_nxdrive_folder_{number}", local_nxdrive_folder)
        setattr(self, f"nxdrive_conf_folder_{number}", nxdrive_conf_folder)
        setattr(self, f"queue_manager_{number}", queue_manager)
        setattr(self, f"sync_root_folder_{number}", sync_root_folder)
        setattr(self, f"local_root_client_{number}", local_root_client)
        setattr(self, f"local_{number}", local)
        setattr(self, f"remote_document_client_{number}", remote_document_client)
        setattr(self, f"remote_{number}", remote)

    def get_bad_remote(self):
        """ A Remote client that will raise some error. """
        return RemoteTest(
            self.nuxeo_url,
            self.user_1,
            "nxdrive-test-administrator-device",
            self.version,
            password=self.password_1,
            dao=self.engine_1._dao,
        )

    def get_local_client(self, path: Path):
        """
        Return an OS specific LocalClient class by default to simulate user actions on:
            - Explorer (Windows)
            - File Manager (macOS)
        On GNU/Linux, there is not specific behavior so the original LocalClient will be used.
        """

        if LINUX:
            client = LocalTest
        elif MAC:
            from .local_client_darwin import MacLocalClient as client
        elif WINDOWS:
            from .local_client_windows import WindowsLocalClient as client

        return client(path)

    def bind_engine(
        self,
        number,
        start_engine=True,
        manager=None,
        folder=None,
        user=None,
        password=None,
    ):
        number_str = str(number)
        manager = manager or getattr(self, f"manager_{number_str}")
        folder = folder or getattr(self, f"local_nxdrive_folder_{number_str}")
        user = user or getattr(self, f"user_{number_str}")
        password = password or getattr(self, f"password_{number_str}")
        engine = manager.bind_server(
            folder, self.nuxeo_url, user, password, start_engine=start_engine
        )

        setattr(self, f"engine_{number}", engine)
        return engine

    def unbind_engine(self, number: int) -> None:
        number_str = str(number)
        engine = getattr(self, f"engine_{number_str}")
        manager = getattr(self, f"manager_{number_str}")
        manager.unbind_engine(engine.uid)
        delattr(self, f"engine_{number_str}")

    def send_bind_engine(self, number: int, start_engine: bool = True) -> None:
        self.app.bindEngine.emit(number, start_engine)

    def send_unbind_engine(self, number: int) -> None:
        self.app.unbindEngine.emit(number)

    def wait_bind_engine(
        self, number: int, timeout: int = DEFAULT_WAIT_SYNC_TIMEOUT
    ) -> None:
        engine = f"engine_{number}"
        for _ in range(timeout):
            if hasattr(self, engine):
                return
            sleep(1)

        self.fail("Wait for bind engine expired")

    def wait_unbind_engine(
        self, number: int, timeout: int = DEFAULT_WAIT_SYNC_TIMEOUT
    ) -> None:
        engine = f"engine_{number}"
        for _ in range(timeout):
            if not hasattr(self, engine):
                return
            sleep(1)

        self.fail("Wait for unbind engine expired")

    def wait(self, retry=3):
        try:
            self.root_remote.wait()
        except Exception as e:
            log.warning(f"Exception while waiting for server: {e!r}")
            # Not the nicest
            if retry > 0:
                log.info("Retry to wait")
                self.wait(retry - 1)

    def wait_sync(
        self,
        wait_for_async=False,
        timeout=DEFAULT_WAIT_SYNC_TIMEOUT,
        fail_if_timeout=True,
        wait_for_engine_1=True,
        wait_for_engine_2=False,
        wait_win=False,
        enforce_errors=True,
    ):
        log.debug("Wait for sync")

        # First wait for server if needed
        if wait_for_async:
            self.wait()

        if wait_win and WINDOWS:
            log.debug("Waiting for Windows delete resolution")
            sleep(WIN_MOVE_RESOLUTION_PERIOD / 1000)

        engine_1 = self.engine_1
        engine_2 = self.engine_2

        if enforce_errors:
            if not self.connected:
                engine_1.syncPartialCompleted.connect(
                    engine_1.get_queue_manager().requeue_errors
                )
                engine_2.syncPartialCompleted.connect(
                    engine_2.get_queue_manager().requeue_errors
                )
                self.connected = True
        elif self.connected:
            engine_1.syncPartialCompleted.disconnect(
                engine_1.get_queue_manager().requeue_errors
            )
            engine_2.syncPartialCompleted.disconnect(
                engine_2.get_queue_manager().requeue_errors
            )
            self.connected = False

        for _ in range(timeout):
            sleep(1)
            if engine_1.is_syncing() or engine_2.is_syncing():
                continue
            elif not wait_for_async:
                return
            elif (
                engine_1._remote_watcher.remote_scan_ended
                and engine_2._remote_watcher.remote_scan_ended
            ):
                return

        if not fail_if_timeout:
            log.debug("Wait for sync timeout")
            return

        count1 = engine_1.get_dao().get_syncing_count()
        count2 = engine_2.get_dao().get_syncing_count()
        err = "Wait for sync timeout has expired"
        if wait_for_engine_1 and count1:
            err += f" for engine 1 (syncing_count={count1})"
        if wait_for_engine_2 and count2:
            err += f" for engine 2 (syncing_count={count2})"
        log.warning(err)

    def make_server_tree(self, deep: bool = True) -> Tuple[int, int]:
        """
        Create some folders on the server.
        Returns a tuple (files_count, folders_count).
        """

        remote = self.remote_document_client_1
        folder_1 = remote.make_folder(self.workspace, "Folder 1")
        folder_2 = remote.make_folder(self.workspace, "Folder 2")

        if deep:
            folder_1_1 = remote.make_folder(folder_1, "Folder 1.1")
            folder_1_2 = remote.make_folder(folder_1, "Folder 1.2")

            # Those 2 attrs are used in test_synchronization.py
            self._duplicate_file_1 = remote.make_file(
                folder_2, "Duplicated File.txt", content=b"Some content."
            )
            self._duplicate_file_2 = remote.make_file(
                folder_2, "Duplicated File.txt", content=b"Other content."
            )

            remote.make_file(folder_1, "File 1.txt", content=b"aaa")
            remote.make_file(folder_1_1, "File 2.txt", content=b"bbb")
            remote.make_file(folder_1_2, "File 3.txt", content=b"ccc")
            remote.make_file(folder_2, "File 4.txt", content=b"ddd")

        remote.make_file(self.workspace, "File 5.txt", content=b"eee")
        return (7, 4) if deep else (1, 2)

    def get_local_child_count(self, path: Path) -> Tuple[int, int]:
        """
        Create some folders on the server.
        Returns a tuple (files_count, folders_count).
        """
        dir_count = file_count = 0
        for _, dirnames, filenames in os.walk(path):
            dir_count += len(dirnames)
            file_count += len(filenames)
        if (path / ".partials").exists():
            dir_count -= 1
        return file_count, dir_count

    def generate_report(self):
        """ Generate a report on failure. """

        if not self.report_path:
            return

        # Track any exception that could happen, specially those we would not
        # see if the test succeed.
        for _, error in getattr(self._outcome, "errors", []):
            exception = str(error[1]).lower()
            message = str(error[2]).lower()
            if "mock" not in exception and "mock" not in message:
                break
        else:
            # No break => no unexpected exceptions
            return

        path = self.report_path / f"{self.id()}-{sys.platform}"
        self.manager_1.generate_report(path)

    def _set_read_permission(self, user, doc_path, grant):
        input_obj = "doc:" + doc_path
        remote = self.root_remote
        if grant:
            remote.execute(
                command="Document.SetACE",
                input_obj=input_obj,
                user=user,
                permission="Read",
                grant=True,
            )
        else:
            remote.block_inheritance(doc_path)

    def assertNxPart(self, path: str, name: str):
        for child in self.local_1.abspath(path).iterdir():
            child_name = child.name
            if len(child_name) < 8:
                continue
            if name is not None and len(child_name) < len(name) + 8:
                continue
            if (
                child_name[0] == "."
                and child_name.endswith(".nxpart")
                and (name is None or child_name[1 : len(name) + 1] == name)
            ):
                self.fail(f"nxpart found in {path!r}")

    def get_dao_state_from_engine_1(self, path: str):
        """
        Returns the pair from dao of engine 1 according to the path.

        :param path: The path to document (from workspace,
               ex: /Folder is converted to /{{workspace_title}}/Folder).
        :return: The pair from dao of engine 1 according to the path.
        """
        abs_path = f"/{self.workspace_title}{path}"
        return self.engine_1.get_dao().get_state_from_local(abs_path)

    def set_readonly(self, user: str, doc_path: str, grant: bool = True):
        """
        Mark a document as RO or RW.

        :param user: Affected username.
        :param doc_path: The document, either a folder or a file.
        :param grant: Set RO if True else RW.
        """
        remote = self.root_remote
        input_obj = "doc:" + doc_path
        if grant:
            remote.execute(
                command="Document.SetACE",
                input_obj=input_obj,
                user=user,
                permission="Read",
            )
            remote.block_inheritance(doc_path, overwrite=False)
        else:
            remote.execute(
                command="Document.SetACE",
                input_obj=input_obj,
                user=user,
                permission="ReadWrite",
                grant=True,
            )


class OneUserTest(TwoUsersTest):
    """ Tests requiring only one user. """

    def setup_method(self, *args, **kwargs):
        kwargs["user_2"] = False
        super().setup_method(*args, **kwargs)

    def wait_sync(
        self,
        wait_for_async=False,
        timeout=DEFAULT_WAIT_SYNC_TIMEOUT,
        fail_if_timeout=True,
        wait_for_engine_1=True,
        wait_win=False,
        enforce_errors=True,
    ):
        log.debug("Wait for sync")

        # First wait for server if needed
        if wait_for_async:
            self.wait()

        if wait_win and WINDOWS:
            log.debug("Waiting for Windows delete resolution")
            sleep(WIN_MOVE_RESOLUTION_PERIOD / 1000)

        engine = self.engine_1

        if enforce_errors:
            if not self.connected:
                engine.syncPartialCompleted.connect(
                    engine.get_queue_manager().requeue_errors
                )
                self.connected = True
        elif self.connected:
            engine.syncPartialCompleted.disconnect(
                engine.get_queue_manager().requeue_errors
            )
            self.connected = False

        for _ in range(timeout):
            sleep(1)
            if engine.is_syncing():
                continue
            elif not wait_for_async:
                return
            elif engine._remote_watcher.remote_scan_ended:
                return

        if not fail_if_timeout:
            log.debug("Wait for sync timeout")
            return

        count = engine.get_dao().get_syncing_count()
        err = "Wait for sync timeout has expired"
        if count:
            err += f" for engine 1 (syncing_count={count})"
        log.warning(err)

# coding: utf-8
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Set, Union

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..constants import APP_NAME
from ..engine.engine import Engine
from ..translator import Translator
from ..utils import get_tree_list, get_tree_size, sizeof_fmt
from .folders_model import FilteredDocuments, FoldersOnly
from .folders_treeview import DocumentTreeView, FolderTreeView

if TYPE_CHECKING:
    from .application import Application  # noqa

__all__ = ("DocumentsDialog", "FoldersDialog")

DOC_URL = "https://doc.nuxeo.com/client-apps/nuxeo-drive-functional-overview/#duplicates-behavior"


class DialogMixin(QDialog):
    """The base class for the tree view window."""

    def __init__(self, application: "Application", engine: Engine) -> None:
        super().__init__(None)

        # Customize the window
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowIcon(application.icon)
        self.setWindowTitle(Translator.get(self.title_label, values=[APP_NAME]))

        # The window doesn't raise on Windows when the app is not in focus,
        # so after the login in the browser, we open the filters window with
        # the "stay on top" hint to make sure it comes back to the front
        # instead of just having a blinking icon in the taskbar.
        self.setWindowFlags(Qt.WindowStaysOnTopHint)

        self.engine = engine
        self.application = application

        # The documents list
        self.tree_view = self.get_tree_view()
        self.tree_view.setContentsMargins(0, 0, 0, 0)

        # Buttons
        self.button_box: QDialogButtonBox = QDialogButtonBox(self)
        self.button_box.setOrientation(Qt.Horizontal)
        self.button_box.setStandardButtons(self.get_buttons())
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        # The content view
        self.vertical_layout = QVBoxLayout(self)

    def get_buttons(self) -> QDialogButtonBox.StandardButtons:
        """Create the buttons to display at the bottom of the window."""
        return QDialogButtonBox.Ok | QDialogButtonBox.Cancel


class DocumentsDialog(DialogMixin):
    """The dialog window for synced documents. Used bu the filters feature."""

    # The windows's title
    title_label = "FILTERS_WINDOW_TITLE"

    def __init__(self, application: "Application", engine: Engine) -> None:
        super().__init__(application, engine)

        self.vertical_layout.addWidget(self.tree_view)
        self.vertical_layout.addWidget(self.button_box)

        # Display something different when the user has no sync root
        self.no_root_label = self.get_no_roots_label()
        self.vertical_layout.insertWidget(0, self.no_root_label)

    def get_buttons(self) -> QDialogButtonBox.StandardButtons:
        """Create the buttons to display at the bottom of the window."""
        # Select/Unselect roots
        self.select_all_state = True
        self.select_all_text = (
            Translator.get("UNSELECT_ALL"),
            Translator.get("SELECT_ALL"),
        )

        buttons = QDialogButtonBox.Ok
        if not self.engine.is_syncing():
            buttons |= QDialogButtonBox.Cancel
            self.select_all_button = self.button_box.addButton(
                self.select_all_text[self.select_all_state], QDialogButtonBox.ActionRole
            )
            self.select_all_button.clicked.connect(self._select_unselect_all_roots)
        return buttons

    def get_tree_view(self) -> Union[QLabel, DocumentTreeView]:
        """Render the documents tree."""

        # Prevent filter modifications while syncing, just display a message to warn the user
        if self.engine.is_syncing():
            label = QLabel(Translator.get("FILTERS_DISABLED"))
            label.setMargin(15)
            label.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            return label

        self.resize(491, 443)
        filters = self.engine.dao.get_filters()
        remote = self.engine.remote
        client = FilteredDocuments(remote, filters)
        tree_view = DocumentTreeView(self, client)
        tree_view.noRoots.connect(self._handle_no_roots)
        return tree_view

    def get_no_roots_label(self) -> QLabel:
        """The contents of the window when there is no sync root."""
        label = QLabel(parent=self)
        text = Translator.get(
            "NO_ROOTS",
            [
                self.engine.server_url,
                "https://doc.nuxeo.com/nxdoc/nuxeo-drive/#synchronizing-a-folder",
            ],
        )
        label.setText(text)
        label.setMargin(15)
        label.setWordWrap(True)
        label.setVisible(False)
        label.setOpenExternalLinks(True)
        label.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        return label

    def _handle_no_roots(self) -> None:
        """When there is no sync root, display an informal message and hide the tree view."""
        self.select_all_button.setVisible(False)
        self.tree_view.setVisible(False)
        self.tree_view.resize(0, 0)
        self.no_root_label.setVisible(True)
        self.setGeometry(self.x(), self.y() + 150, 491, 200)

    def accept(self) -> None:
        """Action to do when the OK button is clicked."""

        # Apply filters if the user has made changes
        if isinstance(self.tree_view, DocumentTreeView):
            self.apply_filters()

        super().accept()

    def apply_filters(self) -> None:
        """Apply changes made by the user."""
        items = sorted(self.tree_view.dirty_items, key=lambda x: x.get_path())
        for item in items:
            path = item.get_path()
            if item.state == Qt.Unchecked:
                self.engine.add_filter(path)
            elif item.state == Qt.Checked:
                self.engine.remove_filter(path)
            elif item.old_state == Qt.Unchecked:
                # Now partially checked and was before a filter

                # Remove current parent filter and need to commit to enable the add
                self.engine.remove_filter(path)

                # We need to browse every child and create a filter for
                # unchecked as they are not dirty but has become root filter
                for child in item.get_children():
                    if child.state == Qt.Unchecked:
                        self.engine.add_filter(child.get_path())

        if not self.engine.is_started():
            self.engine.start()

    def _select_unselect_all_roots(self, _: Qt.CheckState) -> None:
        """The Select/Unselect all roots button."""
        state = Qt.Checked if self.select_all_state else Qt.Unchecked

        roots = sorted(self.tree_view.client.roots, key=lambda x: x.get_path())
        for num, root in enumerate(roots):
            index = self.tree_view.model().index(num, 0)
            item = self.tree_view.model().itemFromIndex(index)
            if item.checkState() != state:
                item.setCheckState(state)
                self.tree_view.update_item_changed(item)

        self.select_all_state = not self.select_all_state
        self.select_all_button.setText(self.select_all_text[self.select_all_state])


class FoldersDialog(DialogMixin):
    """The dialog window for folderish documents. Used bu the Direct Transfer feature."""

    # The windows's title
    title_label = "DIRECT_TRANSFER_WINDOW_TITLE"

    def __init__(
        self, application: "Application", engine: Engine, path: Optional[Path]
    ) -> None:
        """*path* is None when the dialog window is opened from a click on the systray menu icon."""

        super().__init__(application, engine)

        self.path: Optional[Path] = None
        self.paths: Set[Path] = set()

        if path:
            # NXDRIVE-2019
            if not path.is_dir():
                self.path = path
                self.paths.add(self.path)

        self.overall_size = self._get_overall_size()
        self.overall_count = self._get_overall_count()

        self.remote_folder_ref = self.engine.dao.get_config(
            "dt_last_remote_location_ref", ""
        )

        self.vertical_layout.addWidget(self._add_group_local())
        self.vertical_layout.addWidget(self._add_group_remote())
        self.vertical_layout.addWidget(self._add_group_options())
        self.vertical_layout.addWidget(self.button_box)

        self.button_ok_state()

        # Open the files selection dialog if there is no pre-selected paths
        if not self.paths:
            self._select_more_files()

    def _add_group_local(self) -> QGroupBox:
        """Group box for source files."""
        groupbox = QGroupBox(Translator.get("SOURCE_FILES"))
        layout = QHBoxLayout()
        groupbox.setLayout(layout)

        self.local_paths_size_lbl = QLabel(sizeof_fmt(self.overall_size))
        self.local_path = QLineEdit()
        self.local_path.setTextMargins(5, 0, 5, 0)
        self.local_path.setText(self._files_display())
        self.local_path.setReadOnly(True)
        button = QPushButton(Translator.get("ADD_FILES"), self)
        button.clicked.connect(self._select_more_files)
        layout.addWidget(self.local_path)
        layout.addWidget(self.local_paths_size_lbl)
        layout.addWidget(button)

        return groupbox

    def _add_group_options(self) -> QGroupBox:
        """Group box for options."""
        groupbox = QGroupBox(Translator.get("OPTIONS"))
        layout = QVBoxLayout()
        groupbox.setLayout(layout)

        sublayout = QHBoxLayout()
        layout.addLayout(sublayout)

        self._add_subgroup_duplicate_behavior(sublayout)

        return groupbox

    def _add_group_remote(self) -> QGroupBox:
        """Group box for the remote folder."""
        groupbox = QGroupBox(Translator.get("SELECT_REMOTE_FOLDER"))
        layout = QVBoxLayout()
        groupbox.setLayout(layout)

        # The remote browser
        layout.addWidget(self.tree_view)

        sublayout = QHBoxLayout()
        layout.addLayout(sublayout)
        label = QLabel(Translator.get("SELECTED_REMOTE_FOLDER"))
        self.remote_folder = QLineEdit()
        self.remote_folder.setStyleSheet("* { background-color: rgba(0, 0, 0, 0); }")
        self.remote_folder.setReadOnly(True)
        self.remote_folder.setFrame(False)
        sublayout.addWidget(label)
        sublayout.addWidget(self.remote_folder)

        # Populate the remote folder with the previously selected, if any
        self.remote_folder.setText(
            self.engine.dao.get_config("dt_last_remote_location", "")
        )

        return groupbox

    def _add_subgroup_duplicate_behavior(self, layout: QHBoxLayout) -> None:
        """Add a sub-group for the duplicates behavior option."""
        label = QLabel(Translator.get("DUPLICATE_BEHAVIOR", [DOC_URL]))
        label.setToolTip(Translator.get("DUPLICATE_BEHAVIOR_TOOLTIP"))
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(True)
        layout.addWidget(label)

        self.cb = QComboBox()
        self.cb.addItem(Translator.get("DUPLICATE_BEHAVIOR_CHOOSE"), "")
        self.cb.addItem(Translator.get("DUPLICATE_BEHAVIOR_CREATE"), "create")
        self.cb.addItem(Translator.get("DUPLICATE_BEHAVIOR_IGNORE"), "ignore")
        self.cb.addItem(Translator.get("DUPLICATE_BEHAVIOR_OVERRIDE"), "override")
        self.cb.currentIndexChanged.connect(self.button_ok_state)
        layout.addWidget(self.cb)

        # Prevent previous objects to take the whole width, that does not render well for human eyes
        layout.addStretch(0)

    def accept(self) -> None:
        """Action to do when the OK button is clicked."""
        super().accept()
        self.engine.direct_transfer_async(
            self.paths,
            self.remote_folder.text(),
            self.remote_folder_ref,
            duplicate_behavior=self.cb.currentData(),
        )

    def button_ok_state(self) -> None:
        """Handle the state of the OK button. It should be enabled when particular criteria are met."""

        # Required criteria:
        #   - at least 1 local path
        #   - a selected remote path
        #   - a selected duplicate behavior
        self.button_box.button(QDialogButtonBox.Ok).setEnabled(
            bool(self.paths)
            and bool(self.remote_folder.text())
            and bool(self.cb.currentData())
        )

    def get_tree_view(self) -> FolderTreeView:
        """Render the folders tree."""
        self.resize(800, 400)
        client = FoldersOnly(self.engine.remote)
        return FolderTreeView(self, client)

    def _files_display(self) -> str:
        """Return the original file or folder to upload and the count of others to proceed."""
        txt = str(self.path or "")
        if self.overall_count > 1:
            txt += f" (+{self.overall_count - 1:,})"
        return txt

    def _get_overall_count(self) -> int:
        """Compute total number of files and folders."""
        return sum(self._get_count(p) for p in self.paths)

    def _get_overall_size(self) -> int:
        """Compute all local paths contents size."""
        return sum(self._get_size(p) for p in self.paths)

    def _get_count(self, path: Path) -> int:
        """Get the children count of a folder or return 1 if a file."""
        if path.is_dir():
            return len(list(get_tree_list(path, "")))
        return 1

    def _get_size(self, path: Path) -> int:
        """Get the local file size or its contents size when a folder."""
        if path.is_dir():
            return get_tree_size(path)
        return path.stat().st_size

    def _process_additionnal_local_paths(self, paths: List[str]) -> None:
        """Append more local paths to the upload queue."""
        if not paths:
            return

        for local_path in paths:
            path = Path(local_path)

            # If .path is None, then pick the first local path to display something useful
            if not self.path:
                self.path = path

            # Prevent to upload twice the same file
            if path in self.paths:
                continue

            # Save the path
            self.paths.add(path)

            # Recompute total size and count
            self.overall_size += self._get_size(path)
            self.overall_count += self._get_count(path)

        # Update labels with new information
        self.local_path.setText(self._files_display())
        self.local_paths_size_lbl.setText(sizeof_fmt(self.overall_size))

        self.button_ok_state()

    def _select_more_files(self) -> None:
        """Choose additional local files to upload."""
        paths, _ = QFileDialog.getOpenFileNames(self, Translator.get("ADD_FILES"))
        self._process_additionnal_local_paths(paths)

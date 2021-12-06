# Copyright (c) 2021 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.
import tempfile
import json

from PyQt5.QtCore import pyqtProperty, pyqtSignal, pyqtSlot, Qt
from typing import Dict, Optional, Set, TYPE_CHECKING

from UM.i18n import i18nCatalog
from UM.Qt.ListModel import ListModel
from UM.TaskManagement.HttpRequestScope import JsonDecoratorScope
from UM.TaskManagement.HttpRequestManager import HttpRequestData, HttpRequestManager
from UM.Logger import Logger
from UM import PluginRegistry

from cura.CuraApplication import CuraApplication
from cura import CuraPackageManager
from cura.UltimakerCloud.UltimakerCloudScope import UltimakerCloudScope  # To make requests to the Ultimaker API with correct authorization.

from .PackageModel import PackageModel
from .Constants import USER_PACKAGES_URL

if TYPE_CHECKING:
    from PyQt5.QtCore import QObject

catalog = i18nCatalog("cura")


class PackageList(ListModel):
    """ A List model for Packages, this class serves as parent class for more detailed implementations.
    such as Packages obtained from Remote or Local source
    """
    PackageRole = Qt.UserRole + 1
    DISK_WRITE_BUFFER_SIZE = 256 * 1024  # 256 KB

    def __init__(self, parent: Optional["QObject"] = None) -> None:
        super().__init__(parent)
        self._manager: CuraPackageManager = CuraApplication.getInstance().getPackageManager()
        self._plugin_registry: PluginRegistry = CuraApplication.getInstance().getPluginRegistry()
        self._account = CuraApplication.getInstance().getCuraAPI().account
        self._error_message = ""
        self.addRoleName(self.PackageRole, "package")
        self._is_loading = False
        self._has_more = False
        self._has_footer = True
        self._to_install: Dict[str, str] = {}
        self.canInstallChanged.connect(self._install)
        self._local_packages: Set[str] = {p["package_id"] for p in self._manager.local_packages}

        self._ongoing_request: Optional[HttpRequestData] = None
        self._scope = JsonDecoratorScope(UltimakerCloudScope(CuraApplication.getInstance()))

    @pyqtSlot()
    def updatePackages(self) -> None:
        """ A Qt slot which will update the List from a source. Actual implementation should be done in the child class"""
        pass

    @pyqtSlot()
    def abortUpdating(self) -> None:
        """ A Qt slot which allows the update process to be aborted. Override this for child classes with async/callback
        updatePackges methods"""
        pass

    def reset(self) -> None:
        """ Resets and clears the list"""
        self.clear()

    isLoadingChanged = pyqtSignal() 

    def setIsLoading(self, value: bool) -> None:
        if self._is_loading != value:
            self._is_loading = value
            self.isLoadingChanged.emit()

    @pyqtProperty(bool, fset = setIsLoading, notify = isLoadingChanged)
    def isLoading(self) -> bool:
        """ Indicating if the the packages are loading
        :return" ``True`` if the list is being obtained, otherwise ``False``
        """
        return self._is_loading

    hasMoreChanged = pyqtSignal()

    def setHasMore(self, value: bool) -> None:
        if self._has_more != value:
            self._has_more = value
            self.hasMoreChanged.emit()

    @pyqtProperty(bool, fset = setHasMore, notify = hasMoreChanged)
    def hasMore(self) -> bool:
        """ Indicating if there are more packages available to load.
        :return: ``True`` if there are more packages to load, or ``False``.
        """
        return self._has_more

    errorMessageChanged = pyqtSignal()

    def setErrorMessage(self, error_message: str) -> None:
        if self._error_message != error_message:
            self._error_message = error_message
            self.errorMessageChanged.emit()

    @pyqtProperty(str, notify = errorMessageChanged, fset = setErrorMessage)
    def errorMessage(self) -> str:
        """ If an error occurred getting the list of packages, an error message will be held here.

        If no error occurred (yet), this will be an empty string.
        :return: An error message, if any, or an empty string if everything went okay.
        """
        return self._error_message

    @pyqtProperty(bool, constant = True)
    def hasFooter(self) -> bool:
        """ Indicating if the PackageList should have a Footer visible. For paginated PackageLists
        :return: ``True`` if a Footer should be displayed in the ListView, e.q.: paginated lists, ``False`` Otherwise"""
        return self._has_footer

    def getPackageModel(self, package_id: str) -> PackageModel:
        index = self.find("package", package_id)
        return self.getItem(index)["package"]

    canInstallChanged = pyqtSignal(str, bool)

    def _install(self, package_id: str, update: bool = False) -> None:
        package_path = self._to_install.pop(package_id)
        Logger.debug(f"Installing {package_id}")
        to_be_installed = self._manager.installPackage(package_path) is not None
        package = self.getPackageModel(package_id)
        if package.can_update and to_be_installed:
            package.can_update = False
        if update:
            package.is_updating = False
        else:
            Logger.debug(f"Setting recently installed for package: {package_id}")
            package.is_recently_managed = True
            package.is_installing = False
        self.subscribeUserToPackage(package_id, str(package.sdk_version))

    def download(self, package_id: str, url: str, update: bool = False) -> None:

        def downloadFinished(reply: "QNetworkReply") -> None:
            self._downloadFinished(package_id, reply, update)

        def downloadError(reply: "QNetworkReply", error: "QNetworkReply.NetworkError") -> None:
            self._downloadError(package_id, update, reply, error)

        HttpRequestManager.getInstance().get(
            url,
            scope = self._scope,
            callback = downloadFinished,
            error_callback = downloadError
        )

    def _downloadFinished(self, package_id: str, reply: "QNetworkReply", update: bool = False) -> None:
        try:
            with tempfile.NamedTemporaryFile(mode = "wb+", suffix = ".curapackage", delete = False) as temp_file:
                bytes_read = reply.read(self.DISK_WRITE_BUFFER_SIZE)
                while bytes_read:
                    temp_file.write(bytes_read)
                    bytes_read = reply.read(self.DISK_WRITE_BUFFER_SIZE)
                Logger.debug(f"Finished downloading {package_id} and stored it as {temp_file.name}")
                self._to_install[package_id] = temp_file.name
                self.canInstallChanged.emit(package_id, update)
        except IOError as e:
            Logger.error(f"Failed to write downloaded package to temp file {e}")
            temp_file.close()
            self._downloadError(package_id, update)

    def _downloadError(self, package_id: str, update: bool = False, reply: Optional["QNetworkReply"] = None, error: Optional["QNetworkReply.NetworkError"] = None) -> None:
        if reply:
            reply_string = bytes(reply.readAll()).decode()
            Logger.error(f"Failed to download package: {package_id} due to {reply_string}")
        package = self.getPackageModel(package_id)
        if update:
            package.is_updating = False
        else:
            package.is_installing = False

    def subscribeUserToPackage(self, package_id: str, sdk_version: str) -> None:
        if self._account.isLoggedIn:
            Logger.debug(f"Subscribing the user for package: {package_id}")
            HttpRequestManager.getInstance().put(
                url = USER_PACKAGES_URL,
                data = json.dumps({"data": {"package_id": package_id, "sdk_version": sdk_version}}).encode(),
                scope = self._scope
            )

    def unsunscribeUserFromPackage(self, package_id: str) -> None:
        if self._account.isLoggedIn:
            Logger.debug(f"Unsubscribing the user for package: {package_id}")
            HttpRequestManager.getInstance().delete(url = f"{USER_PACKAGES_URL}/{package_id}", scope = self._scope)

    # --- Handle the manage package buttons ---

    def _connectManageButtonSignals(self, package: PackageModel) -> None:
        package.installPackageTriggered.connect(self.installPackage)
        package.uninstallPackageTriggered.connect(self.uninstallPackage)
        package.updatePackageTriggered.connect(self.installPackage)
        package.enablePackageTriggered.connect(self.enablePackage)
        package.disablePackageTriggered.connect(self.disablePackage)

    @pyqtSlot(str)
    def installPackage(self, package_id: str) -> None:
        package = self.getPackageModel(package_id)
        package.is_installing = True
        url = package.download_url
        Logger.debug(f"Trying to download and install {package_id} from {url}")
        self.download(package_id, url, False)

    @pyqtSlot(str)
    def uninstallPackage(self, package_id: str) -> None:
        Logger.debug(f"Uninstalling {package_id}")
        package = self.getPackageModel(package_id)
        package.is_installing = True
        self._manager.removePackage(package_id)
        self.unsunscribeUserFromPackage(package_id)
        package.is_installing = False
        package.is_recently_managed = True

    @pyqtSlot(str)
    def updatePackage(self, package_id: str) -> None:
        package = self.getPackageModel(package_id)
        package.is_updating = True
        self._manager.removePackage(package_id, force_add = True)
        url = package.download_url
        Logger.debug(f"Trying to download and update {package_id} from {url}")
        self.download(package_id, url, True)

    @pyqtSlot(str)
    def enablePackage(self, package_id: str) -> None:
        package = self.getPackageModel(package_id)
        package.is_enabling = True
        Logger.debug(f"Enabling {package_id}")
        self._plugin_registry.enablePlugin(package_id)
        package.is_active = True
        package.is_enabling = False

    @pyqtSlot(str)
    def disablePackage(self, package_id: str) -> None:
        package = self.getPackageModel(package_id)
        package.is_enabling = True
        Logger.debug(f"Disabling {package_id}")
        self._plugin_registry.disablePlugin(package_id)
        package.is_active = False
        package.is_enabling = False

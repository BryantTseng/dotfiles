import sys
from loguru import logger
from handlers.handler import Handler
import distro
import os


class PlatformHandler(Handler):

    supported_platform = ["linux"]
    current_platform = str
    current_distro = str

    def __init__(self):
        self.current_platform = sys.platform
        self.current_distro = distro.name()
        return

    def get_current_platform(self) -> str:
        return self.current_platform

    def get_current_distro(self) -> str:
        return self.current_distro
    def check_directory_or_file_exist(self, path)->bool:
        if os.path.exists(path):
            return True
        return False
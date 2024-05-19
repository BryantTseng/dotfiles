import os
from loguru import logger
from handlers.handler import Handler
import subprocess

class ZshHandler(Handler):
    supported_platform = ["linux"]
    supported_distro = ["openSUSE Tumbleweed", "Debian GNU/Linux"]

    def __init__(self):
        return

    def shell_is_zsh(self) -> bool:
        logger.debug("checking envvar $SHELL")
        env_shell = os.environ["SHELL"]
        if "zsh" in env_shell:
            return True
        else:
            return False

    def install_zsh(self, plt:str, distro:str):
        logger.info(f"installing zsh for {plt}:{distro}")
        if plt == "linux":
            if distro == "openSUSE Tumbleweed":
                pass
            elif distro == "Debian GNU/Linux":
                self.run_shell_cmd("sudo apt-get -y install zsh")
                pass



    def install_oh_my_zsh(self):
        return

    def install_oh_my_zsh_plugin(self):
        return

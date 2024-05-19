import os
from loguru import logger
from handlers.handler import Handler


class ZshHandler(Handler):
    supported_platform = ["linux"]
    supported_distro = ["openSUSE Tumbleweed", "Debian GNU/Linux"]
    def __init__(self):
        return

    def shell_is_zsh(self) -> bool:
        logger.debug("[zsh] checking envvar $SHELL")
        env_shell = os.environ["SHELL"]
        if "zsh" in env_shell:
            return True
        else:
            return False

    def install_zsh(self, plt:str, distro:str):
        logger.info(f"[zsh] installing zsh for {plt}:{distro}")
        if plt == "linux":
            if distro == "openSUSE Tumbleweed":
                pass
            elif distro == "Debian GNU/Linux":
                self.run_shell_cmd("sudo apt-get -y install zsh")
                



    def install_oh_my_zsh(self):
        logger.info("[zsh][oh-my-zsh] installing zsh-syntax-highlighting")
        self.run_shell_cmd("""
                            sh -c "$(wget https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh -O -)"
                           """)
        return

    def install_oh_my_zsh_plugin(self):
        # zsh-syntax-highlighting
        logger.info("[zsh][oh-my-zsh][plugin] installing zsh-syntax-highlighting")
        self.run_shell_cmd("""
                           git clone https://github.com/zsh-users/zsh-syntax-highlighting.git ~/.oh-my-zsh/custom/plugins/zsh-syntax-highlighting
                           """)
        # zsh-autosuggestions
        logger.info("[zsh][oh-my-zsh][plugin] installing zsh-syntax-autosuggestions")
        self.run_shell_cmd("""
                           git clone https://github.com/zsh-users/zsh-autosuggestions ~/.oh-my-zsh/custom/plugins/zsh-autosuggestions
                           """)
        

    def install_oh_my_zsh_theme(self):
        # spaceship
        logger.info("[zsh][oh-my-zsh][theme] installing spaceship theme")
        self.run_shell_cmd("""
                           git clone https://github.com/spaceship-prompt/spaceship-prompt.git "~/.oh-my-zsh/custom/themes/spaceship-prompt" --depth=1
                           """)
        logger.info("[zsh][oh-my-zsh][theme] installing spaceship theme soft link")
     
        self.run_shell_cmd("""
                           ln -s "~/.oh-my-zsh/themes/spaceship-prompt/spaceship.zsh-theme" "~/.oh-my-zsh/custom/themes/spaceship.zsh-theme"
                           """)
        
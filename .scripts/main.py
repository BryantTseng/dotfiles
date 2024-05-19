import click
from loguru import logger

from handlers.zsh import ZshHandler
from handlers.platform import PlatformHandler
import shutil

@click.group()
def cli():
    pass


@click.command()
def zsh():
    platform = PlatformHandler()
    zsh = ZshHandler()

    logger.info("handling zsh...")
    logger.info("checking compatibility for zsh...")
    if not zsh.check_platform_compatibility(platform.get_current_platform()):
        logger.error(
            f"platform {platform.get_current_platform()} not supported, supported: {zsh.get_supported_platforms()}"
        )
        exit(1)
    if not zsh.check_distro_compatibility(platform.get_current_distro()):
        logger.error(
            f"platform {platform.get_current_distro()} not supported, supported: {zsh.get_supported_distro()}"
        )
        exit(1)

            

    if zsh.shell_is_zsh():
        logger.info("shell is zsh already, skip zsh installation...")
    else:
        logger.info("installing zsh...")
        zsh.install_zsh(platform.get_current_platform(), platform.get_current_distro())

    skip_oh_my_zsh=False
    if platform.check_directory_or_file_exist("~/.oh-my-zsh"):
        if click.confirm("~/.oh-my-zsh directory already exist, delete the directory?", default=True):
            shutil.rmtree("~/.oh-my-zsh")
        else:
            logger.info("skip oh-my-zsh installation since ~/.oh-my-zsh is existed...")
            skip_oh_my_zsh=True
    if not skip_oh_my_zsh:
        zsh.install_oh_my_zsh()
cli.add_command(zsh)

cli()

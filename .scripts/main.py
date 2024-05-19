import click
from loguru import logger

from handlers.zsh import ZshHandler
from handlers.platform import PlatformHandler


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


cli.add_command(zsh)

cli()

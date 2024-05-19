import subprocess
import sys
from loguru import logger

class Handler:
    supported_platform: list[str]
    supported_distro: list[str]

    def get_supported_platforms(self) -> list[str]:
        return self.supported_platform

    def get_supported_distro(self) -> list[str]:
        return self.supported_distro

    def check_platform_compatibility(self, plt) -> bool:
        if plt not in self.supported_platform:
            return False
        return True

    def check_distro_compatibility(self, distro) -> bool:
        if distro not in self.supported_distro:
            return False
        return True
    def run_shell_cmd(self, cmd):
        p = subprocess.Popen(cmd, shell=True, stderr=subprocess.PIPE)
        
        for line in iter(p.stderr.readline, b''):
            logger.info(f"{line}")
        p.stderr.close()
        p.wait()
"""Fixed browser mounts. Only the runtime edge may supply the exact alias tuple."""
from dataclasses import dataclass

HZ_BASE = "https://ixicai.cn/hz/montage"
HZ_PREFIX = "/hz/montage"
HZ_IDENTITY = "https://ixicai.cn/hz/user/api"


@dataclass(frozen=True)
class BrowserRealm:
    id: str
    base: str
    identity: str
    cookie: str
    state_cookie: str
    suppress_cookie: str
    prefix: str = ""

    @property
    def origin(self):
        from urllib.parse import urlsplit
        parts = urlsplit(self.base)
        return parts.scheme + "://" + parts.netloc

    def path(self, value):
        return self.prefix + value


def canonical(config):
    return BrowserRealm("canonical", config.public_origin, config.issuer,
                        "__Host-dofe_montage_sid", "__Host-dofe_montage_state", "__Host-dofe_montage_logout")


def hz(config):
    if config.public_origin != "https://montage.hozonauto.com" or config.issuer != "https://user.hozonauto.com/api":
        raise ValueError("Alias requires the fixed Hozon authority")
    return BrowserRealm("hz", HZ_BASE, HZ_IDENTITY, "__Host-hz-dofe_rp_montage",
                        "__Host-hz-dofe_rp_montage_state", "__Host-hz-dofe_rp_montage_logout", HZ_PREFIX)


def resolve(request, config):
    if "x-external-base" in request.headers or "x-forwarded-prefix" in request.headers:
        expected = {"host": "montage.hozonauto.com", "x-external-base": HZ_BASE,
                    "x-forwarded-host": "ixicai.cn", "x-forwarded-prefix": HZ_PREFIX,
                    "x-forwarded-proto": "https"}
        if any(len(request.headers.getlist(key)) != 1 or request.headers.get(key) != value for key, value in expected.items()):
            raise ValueError("Untrusted browser mount")
        return hz(config)
    if request.url.path == HZ_PREFIX or request.url.path.startswith(HZ_PREFIX + "/"):
        raise ValueError("Alias mount metadata missing")
    return canonical(config)

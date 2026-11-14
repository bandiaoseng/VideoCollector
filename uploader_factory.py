"""Choose one upload provider without silently falling back after bad setup."""


def create_uploader(config, progress=lambda message: None):
    if 'quark_web' in config:
        from quark_web_uploader import WebUploader
        return WebUploader(config, progress)
    from collector import QuarkUploader
    return QuarkUploader(config)

"""Selenium WebDriver factory.

Locally this starts Chrome via Selenium Manager (which resolves a matching
chromedriver automatically); in Docker it connects to a Selenium Grid /
standalone-chrome container (``SELENIUM_REMOTE_URL``). Design rules:

* Browsers are expensive: they're used only where a real JS engine is
  required (session bootstrap, API discovery, DOM fallback) - bulk data goes
  over plain HTTP.
* Explicit waits only (``WebDriverWait``); implicit waits stay at 0 because
  mixing the two produces unpredictable timeouts.
* ``driver.quit()`` always runs - leaked Chrome processes are the most common
  way Selenium scrapers take down a host.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.remote.command import Command
from selenium.webdriver.remote.webdriver import WebDriver


def build_chrome_options(*, headless: bool, capture_network: bool, block_images: bool) -> ChromeOptions:
    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    for arg in (
        "--no-sandbox",  # required when Chrome runs as root inside containers
        "--disable-dev-shm-usage",  # /dev/shm is 64MB in Docker; use /tmp instead
        "--disable-gpu",
        "--disable-extensions",
        "--window-size=1366,900",
        f"--user-agent={settings.CRAWLER['USER_AGENT']}",
    ):
        options.add_argument(arg)
    # Return control after DOMContentLoaded; SPA content is awaited explicitly.
    options.page_load_strategy = "eager"
    if block_images:
        options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    if capture_network:
        # ChromeDriver mirrors DevTools Network.* events into the "performance" log.
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return options


@contextmanager
def chrome_driver(
    *,
    capture_network: bool = False,
    block_images: bool = True,
    page_load_timeout: int = 30,
    headless: bool | None = None,
) -> Iterator[WebDriver]:
    conf = settings.CRAWLER
    options = build_chrome_options(
        headless=conf["SELENIUM_HEADLESS"] if headless is None else headless,
        capture_network=capture_network,
        block_images=block_images,
    )
    remote_url = conf["SELENIUM_REMOTE_URL"]
    driver = (
        webdriver.Remote(command_executor=remote_url, options=options)
        if remote_url
        else webdriver.Chrome(options=options)
    )
    try:
        driver.set_page_load_timeout(page_load_timeout)
        yield driver
    finally:
        driver.quit()


def get_log(driver: WebDriver, log_type: str) -> list[dict]:
    """``driver.get_log`` exists on local Chrome drivers but not on ``webdriver.Remote``."""
    if hasattr(driver, "get_log"):
        return driver.get_log(log_type)
    return driver.execute(Command.GET_LOG, {"type": log_type})["value"]


def execute_cdp(driver: WebDriver, cmd: str, params: dict | None = None) -> dict:
    """Run a Chrome DevTools Protocol command on a local *or* remote Chrome."""
    if hasattr(driver, "execute_cdp_cmd"):
        return driver.execute_cdp_cmd(cmd, params or {})
    return driver.execute("executeCdpCommand", {"cmd": cmd, "params": params or {}})["value"]

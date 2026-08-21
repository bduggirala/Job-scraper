"""Playwright-based browser fallback for career sites without an API."""

from browser.playwright_scraper import PlaywrightResult, scrape_with_playwright, shutdown_browsers

__all__ = ["PlaywrightResult", "scrape_with_playwright", "shutdown_browsers"]

import os
import asyncio
from browser_use import Agent, Browser


async def main():
    browser = Browser(
        "chromium",
        headless=True,
    )

    sensitive_data = {
        "x_username": os.environ.get("SITE_USERNAME", ""),
        "x_password": os.environ.get("SITE_PASSWORD", ""),
    }

    task_instructions = """
    BROWSER WORKFLOW:
    1. Open the website by running: new_tab("https://" + "sp1" + "der.framer.photos")
    2. Wait 3 seconds for the page to load.
    3. If you see a login screen:
       - Click on the username/email input box first to focus it, then call type_secret('x_username').
       - Click on the password input box first to focus it, then call type_secret('x_password').
       - Click the Log In button and wait 3 seconds for the page to load.
       - (Note: type_secret is already a built-in global function; do NOT try to import it.)
    4. Look at the "Community" tab/button and click on it to view the servers.
    5. Check the server list for any servers with unread badges (or check the main server if no badges appear).
    6. Click into the active server, read the recent chat messages from other users, and evaluate them:
       - Decide based on your Aegis AI rules below whether to reply, flag an issue to @sigma, or stay silent.
       - If you decide to reply or flag to @sigma, click the chat message input box, type your response, and press Enter (or click Send).
       - Wait 2 seconds after sending to make sure it posts.
    7. Finish the task and summarize what you saw and posted."""

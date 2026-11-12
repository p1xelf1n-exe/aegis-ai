import os
import asyncio
from browser_use import Agent, Browser


async def main():
    # Launches local headless Chromium on the GitHub Actions server
    browser = Browser(
        "chromium",
        headless=True,
        allowed_domains=["YOUR_WEBSITE_DOMAIN.COM"],
    )

    # Keeps credentials hidden from the AI model and terminal logs
    sensitive_data = {
        "x_username": os.environ.get("SITE_USERNAME", ""),
        "x_password": os.environ.get("SITE_PASSWORD", ""),
    }

    task_instructions = """
    1. Navigate to https://YOUR_WEBSITE_URL_HERE.
    2. If you see a login screen, use type_secret('x_username') in the username/email field, type_secret('x_password') in the password field, and click Log In.
    3. Once logged in, look for any new, unread user messages or support tickets in the inbox/chat.
    4. If there are no new messages, stop immediately and finish the task.
    5. For each unread message, read it carefully and decide:
       - ROUTINE QUESTION: If it is answered by the FAQ below, click the reply box, type a polite, concise answer, and send it.
       - ESCALATE: If the user asks for a refund, reports a technical bug, sounds angry, or asks something NOT covered in the FAQ, click 'Escalate' (or leave it unread) so a higher authority/human handles it.
    
    FAQ & RULES:
    - [Add rule 1 here]
    - [Add rule 2 here]
    - Never guess or make up policies not listed above.
    """

    # Automatically reads OPENROUTER_API_KEY from environment
    agent = Agent(
        task=task_instructions,
        llm="openrouter:openrouter/free",
        browser=browser,
        sensitive_data=sensitive_data,
        use_vision=False,
    )

    try:
        history = await agent.run(max_steps=20)
        print("Run summary:", history.final_result())
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

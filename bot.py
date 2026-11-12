import os
import json
import asyncio
from browser_use import Agent, Browser, ChatOpenRouter


def prepare_storage_state():
    """Converts your SITE_COOKIES secret into Playwright's storage_state format."""
    raw_cookies = os.environ.get("SITE_COOKIES")
    if not raw_cookies:
        return None

    data = json.loads(raw_cookies)
    if isinstance(data, list):
        for cookie in data:
            if "sameSite" in cookie and cookie["sameSite"] not in ["Strict", "Lax", "None"]:
                cookie["sameSite"] = "Lax"
        storage_data = {"cookies": data, "origins": []}
    else:
        storage_data = data

    state_path = "auth_state.json"
    with open(state_path, "w") as f:
        json.dump(storage_data, f)
    return state_path


async def main():
    # 1. Connect OpenRouter's free router (automatically picks an active free model)
    llm = ChatOpenRouter(
        model="openrouter/free",
        api_key=os.environ.get("OPENROUTER_API_KEY"),
    )

    # 2. Load your saved website login session
    state_path = prepare_storage_state()
    browser = Browser(
        headless=True,
        storage_state=state_path,
        allowed_domains=["YOUR_WEBSITE_DOMAIN.COM"],  # Security lock: keeps bot on your site
    )

    # 3. Define your support rules and escalation triggers
    task_instructions = """
    1. Navigate to https://YOUR_WEBSITE_URL_HERE.
    2. Look for any new, unread user messages or support tickets in the inbox/chat.
    3. If there are no new messages, stop immediately and finish the task.
    4. For each unread message, read it carefully and decide:
       - ROUTINE QUESTION: If it is answered by the FAQ below, click the reply box, type a polite, concise answer, and send it.
       - ESCALATE: If the user asks for a refund, reports a technical bug, sounds angry, or asks something NOT covered in the FAQ, click 'Escalate' (or leave it unread) so a higher authority/human handles it.
    
    FAQ & RULES:
    - [Add rule 1 here, e.g., Hours are 9 AM to 5 PM EST]
    - [Add rule 2 here, e.g., Password resets can be done at /reset]
    - Never guess or make up policies not listed above.
    """

    agent = Agent(
        task=task_instructions,
        llm=llm,
        browser=browser,
        use_vision=False,  # Keeps requests fast and compatible with all free text/tool models
        max_steps=15,
    )

    try:
        history = await agent.run()
        print("Run summary:", history.final_result())
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

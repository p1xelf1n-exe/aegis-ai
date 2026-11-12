import os
import asyncio
from browser_use import Agent, Browser, ChatOpenRouter


async def main():
    llm = ChatOpenRouter(
        model="openrouter/free",
        api_key=os.environ.get("OPENROUTER_API_KEY"),
    )

    browser = Browser(
        headless=True,
        allowed_domains=["framer.com"],
    )

    # Keeps credentials hidden from the AI model and terminal logs
    sensitive_data = {
        "x_username": os.environ.get("SITE_USERNAME", ""),
        "x_password": os.environ.get("SITE_PASSWORD", ""),
    }

    task_instructions = """
    1. Navigate to https://sp1der.framer.photos.
    2. If you see a login screen, enter x_username into the username/email field, enter x_password into the password field, and click Log In.
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

    agent = Agent(
        task=task_instructions,
        llm=llm,
        browser=browser,
        sensitive_data=sensitive_data,
        use_vision=False,
        max_steps=20,
    )

    try:
        history = await agent.run()
        print("Run summary:", history.final_result())
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

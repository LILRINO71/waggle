"""Set up urgent push alerts on the phone: sends the private ntfy channel to Telegram, then a test alarm.

    python -m waggle.pushsetup
"""
import sys

from . import notify
from .config import Settings

STEPS = """🐝 Urgent alerts setup (free, about 1 minute)
1. Install "ntfy" from the App Store (free, by Philipp Heckel).
2. Open it, tap +, and subscribe to this topic (copy it exactly):
{topic}
Leave "Use another server" off.
3. When iPhone asks, Allow notifications, and turn on Time Sensitive for ntfy
   (Settings > Notifications > ntfy > Time Sensitive). That lets urgent alerts break through Focus.
I'll send a test alarm in a moment. Keep the topic private: anyone who has it can read your alerts."""


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    s = Settings.load()
    topic = notify.ntfy_topic(s)
    sent = notify.to_phone(STEPS.format(topic=topic), s)
    print("instructions sent to Telegram:", sent)
    ok = notify.to_ntfy("Test alarm: if you can read this, urgent alerts reach your phone.",
                        title="🚨 Waggle test", priority=5, tags=("rotating_light",), settings=s)
    print("test alarm posted:", ok)


if __name__ == "__main__":
    main()

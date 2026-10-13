"""Agent launcher for a supervised workspace; exiting never signals the runtime PID 1."""

from panopticon.container.agent import main

if __name__ == "__main__":
    main(on_exit=lambda: None)

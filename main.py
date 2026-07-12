def set_switch_state(id: int, on: bool):
    pass


def set_switch_states(states: list[dict]):
    for state in states:
        set_switch_state(state["id"], state["on"])


def main():
    pass


if __name__ == "__main__":
    main()

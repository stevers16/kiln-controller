# test_button.py -- manual test for display timeout, wake, and page cycling
# Run on Pico: mpremote run test_button.py
# Timeout is 10s so you don't wait long. Press GP10 button to wake/cycle.

from lib.display import Display, Color

d = Display(timeout_s=10)


def page_status():
    d.draw_text(0, 0, "STATUS", Color.GREEN, size=48)
    d.draw_text(0, 60, "Temp: 42C  RH: 35%", Color.WHITE, size=24)


def page_sensors():
    d.draw_text(0, 0, "SENSORS", Color.CYAN, size=48)
    d.draw_text(0, 60, "Lumber: 18% MC", Color.WHITE, size=24)
    d.draw_text(0, 90, "Intake: 28C 55%", Color.WHITE, size=24)


def page_schedule():
    d.draw_text(0, 0, "SCHEDULE", Color.ORANGE, size=48)
    d.draw_text(0, 60, "Stage 2 of 4", Color.WHITE, size=24)
    d.draw_text(0, 90, "Target: 55C / 30%", Color.WHITE, size=24)


d.register_page("status", page_status)
d.register_page("sensors", page_sensors)
d.register_page("schedule", page_schedule)

d.show_page("status")
print("Showing 'status' page. Display blanks in 10s.")
print("Press button to wake / cycle pages. Ctrl-C to quit.")

while True:
    pressed = d.tick()
    if pressed:
        print(f"Button -> page: {d.current_page_name}")

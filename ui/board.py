# ui/board.py
# Styling for the drag-and-drop boards (streamlit-sortables renders inside its own
# frame, so it needs its own font and text direction).

from ui.i18n import is_rtl


def board_style(numbered=False):
    direction = "rtl" if is_rtl() else "ltr"
    css = f"""
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600&display=swap');
body, .sortable-component {{ font-family: 'IBM Plex Sans Arabic', 'Segoe UI', Tahoma, sans-serif;
                             direction: {direction}; }}
.sortable-component {{ background: transparent; border: 0; padding: 0; gap: 12px; }}
.sortable-container {{ background: #FFFFFF; border: 1px solid #DCE1DA; border-radius: 14px;
                       padding: 6px; min-height: 80px; flex: 1; counter-reset: rank; }}
.sortable-container-header {{ background: transparent; color: #14213D; font-weight: 600;
                              padding: 8px 10px 4px; }}
.sortable-item {{ background: #F2F4F1; color: #14213D; border: 1px solid #DCE1DA;
                  border-radius: 10px; padding: 8px 12px; margin: 4px; font-weight: 500;
                  cursor: grab; }}
.sortable-item:hover, .sortable-item:focus, .sortable-item:active {{ border-color: #14213D;
  color: #14213D !important; }}
.sortable-item:active {{ cursor: grabbing; }}
"""
    if numbered:
        css += """
.sortable-item::before { counter-increment: rank; content: counter(rank); display: inline-block;
  min-width: 1.6em; height: 1.6em; line-height: 1.6em; text-align: center; border-radius: 50%;
  background: #14213D; color: #fff; font-size: .8em; margin-inline-end: .7em; }
"""
    return css


def column_tint(index, color, soft):
    """Colour the header strip of the n-th board column (1-based)."""
    return f"""
.sortable-container:nth-of-type({index}) {{ border-top: 4px solid {color}; }}
.sortable-container:nth-of-type({index}) .sortable-item {{ background: {soft}; }}
"""

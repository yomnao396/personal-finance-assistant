# ui/forms.py
# Fresh, empty fields after a successful save: each form's widget keys carry a
# counter, and bumping it makes Streamlit draw new widgets with their defaults.
# (A widget's own value cannot be reset after it has been drawn in a run.)

import streamlit as st


def form_key(form, name):
    return f"{form}_{name}_{st.session_state.get(f'_form_{form}', 0)}"


def reset_form(form):
    st.session_state[f"_form_{form}"] = st.session_state.get(f"_form_{form}", 0) + 1

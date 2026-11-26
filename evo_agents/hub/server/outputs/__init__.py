"""The outputs of runs on the hub (``evo_agents.hub.outputs``): the files a run's agent makes, recorded from its
machine, listed and read by the run's readers, carried by questions and chat messages, served from the hub's own
origin, and told to the run's owner in a notice; and the notification settings of a member.

Layered as ``routes`` (the HTTP routes, which pass the request's parts on) over ``service`` (the checks and the
transactions) over ``repository`` (every SQL query) over ``models`` (the bodies, answers and route parameters); the
import-linter contract hub-outputs-layers holds it.
"""

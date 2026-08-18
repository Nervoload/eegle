"""Routed Workbench pages."""

from demos.workbench.views.apparatus import ApparatusPage
from demos.workbench.views.build import BuildPage
from demos.workbench.views.design import DesignPage
from demos.workbench.views.experiments import ExperimentsPage
from demos.workbench.views.replay import ReplayPage
from demos.workbench.views.run import RunPage
from demos.workbench.views.sessions import SessionsPage

__all__ = [
    "ApparatusPage",
    "BuildPage",
    "DesignPage",
    "ExperimentsPage",
    "ReplayPage",
    "RunPage",
    "SessionsPage",
]

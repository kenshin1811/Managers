"""The decision engine.

Split deliberately into two halves:

* :mod:`app.engine.guardrails` and :mod:`app.engine.candidates` are **pure**.
  They read a snapshot of the workforce and return verdicts and scores.  They
  never write to the database and never send a message, which is what makes
  every staffing rule testable in isolation.
* :mod:`app.engine.reassignment` plans and then executes.  ``plan_for_leave``
  is pure and returns a :class:`~app.engine.types.ReassignmentPlan`;
  ``execute_plan`` is the only place that commits changes and dispatches
  notifications.
"""

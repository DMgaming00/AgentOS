"""Release Center: environments, staged canary rollouts, promotion gates and deterministic rollback rules.

"Production" here is a *production-style local simulation*: the agent's own active and canary pointers inside this
deployment. Nothing in this package deploys to, or implies, a hosted production environment.
"""

ENVIRONMENTS = ("development", "staging", "production")
DEFAULT_SCHEDULE = [5, 10, 25, 50, 100]
ALLOWED_PERCENTS = (5, 10, 25, 50, 100)

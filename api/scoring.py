import math

# Confidence z-score for the Wilson interval. z = 1.96 -> 95% (spec §3).
WILSON_Z = 1.96


def wilson_lower_bound(ups: int, downs: int) -> float:
    """Lower bound of the Wilson score confidence interval for a Bernoulli
    parameter, used as the "Top Rated" sort key (spec §3).

    With `up` positive votes out of `n = up + down` total:

        phat   = up / n
        wilson = (phat + z^2/(2n) - z*sqrt((phat*(1-phat) + z^2/(4n)) / n)) / (1 + z^2/n)

    Returns 0.0 when there are no votes (n == 0), which is also the stored
    default for a never-voted pack.
    """
    n = ups + downs
    if n <= 0:
        return 0.0
    z = WILSON_Z
    phat = ups / n
    return (
        phat
        + z * z / (2 * n)
        - z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    ) / (1 + z * z / n)
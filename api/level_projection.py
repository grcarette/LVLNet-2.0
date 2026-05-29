"""Shared MongoDB aggregation stages for resolving a `levels` document into the
client-facing read shape (creators resolved to {id, username}, plus a thumbnail
URL when the level has a stored thumbnail).

Kept in its own module so both the level routes and the pack-scoped level
resolver in the packs router can reuse it without creating an import cycle
between those two route modules.
"""


def creator_lookup_stages() -> list:
    """$lookup + $project stages that turn a raw `levels` document into the read
    shape the level endpoints return.

    Creators are resolved two ways and concatenated:
      * discord creators: the int discord_ids in `creators`, joined to `users`.
      * a gsid author (game-created levels): surfaced from `author_gsid` /
        `author_name` as a single {id, username} entry, so a game-created level
        still shows a human creator name without a Discord account.

    `hidden` and `thumbnailUrl` are *additive* fields; older clients that don't
    map them simply ignore them. These stages do NOT filter on `hidden` — each
    caller adds its own $match (e.g. the discovery endpoints keep excluding
    hidden; the pack-scoped resolver intentionally does not).
    """
    return [
        {
            "$lookup": {
                "from": "users",
                "localField": "creators",
                "foreignField": "discord_id",
                "as": "creator_objects",
            }
        },
        {
            "$project": {
                "_id": 0,
                "name": 1,
                "code": 1,
                "imgur_url": 1,
                "mode": 1,
                "tournament_legal": 1,
                "hidden": {"$ifNull": ["$hidden", False]},
                "thumbnailUrl": {
                    "$cond": [
                        {"$ifNull": ["$thumbnail", False]},
                        {"$concat": ["/levels/", "$code", "/thumbnail"]},
                        None,
                    ]
                },
                "creators": {
                    "$concatArrays": [
                        {
                            "$map": {
                                "input": "$creator_objects",
                                "as": "user",
                                "in": {
                                    "id": "$$user.discord_id",
                                    "username": "$$user.username",
                                },
                            }
                        },
                        {
                            "$cond": [
                                {"$ifNull": ["$author_gsid", False]},
                                [
                                    {
                                        "id": "$author_gsid",
                                        "username": {
                                            "$ifNull": ["$author_name", "Player"]
                                        },
                                    }
                                ],
                                [],
                            ]
                        },
                    ]
                },
            }
        },
    ]
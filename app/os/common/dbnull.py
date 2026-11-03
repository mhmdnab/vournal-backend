"""Prisma's DbNull for a nullable Json column. prisma-client-py can write JSON null
(Json(None)) but not a database NULL, which is what NestJS stored when it cleared a
prayer anchor. Put DB_NULL in an update's data, then:

    nulls = pop_db_nulls(data)
    await client.task.update(where={"id": id}, data=data)
    await set_db_null(client, "Task", id, nulls)   # same client: same transaction
"""

DB_NULL = object()


def pop_db_nulls(data: dict) -> list[str]:
    columns = [k for k, v in data.items() if v is DB_NULL]
    for column in columns:
        del data[column]
    return columns


async def set_db_null(client, table: str, id: str, columns: list[str]) -> None:
    for column in columns:  # names come from our own code, never from a request
        await client.execute_raw(f'UPDATE "{table}" SET "{column}" = NULL WHERE "id" = $1', id)

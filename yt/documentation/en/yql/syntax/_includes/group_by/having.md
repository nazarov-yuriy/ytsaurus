---
vcsPath: ydb/docs/ru/core/yql/reference/yql-core/syntax/_includes/group_by/having.md
sourcePath: ydb/docs/ru/core/yql/reference/yql-core/syntax/_includes/group_by/having.md
---
## HAVING {#having}

Filtering a `SELECT` based on the aggregate function calculation results. The syntax is similar to the [`WHERE`](../../select.md#where) clause.

**Example**

```yql
SELECT
    key
FROM my_table
GROUP BY key
HAVING COUNT(value) > 100;
```
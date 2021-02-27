import gqljit
import graphql as g

schema = g.GraphQLSchema(
    query=g.GraphQLObjectType(
        name="Query",
        fields={
            "viewer": g.GraphQLField(
                g.GraphQLObjectType(
                    name="Viewer",
                    fields={
                        "Hello": g.GraphQLField(
                            g.GraphQLString,
                            resolve=lambda root, info: "World",
                        ),
                    },
                ),
                resolve=lambda root, info: None,
            ),
        },
    ),
)

print(
    g.graphql_sync(
        schema,
        "query { viewer { Hello } viewer2: viewer { Hello } }",
        execution_context_class=gqljit.JITExecutionContext,
        root_value={"viewer": {"Hello": "world!"}},
    )
)

import gqljit
import graphql as g


def breakit(*args):
    raise BaseException("yo!")


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
                            # resolve=lambda root, info: root["Hello"].upper(),
                            resolve=breakit,
                        ),
                    },
                ),
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

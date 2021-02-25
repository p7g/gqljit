# gqljit

The idea: A graphql-core ExecutionContext which JIT-compiles queries rather than interpreting them every time.

## Still to do

A lot.

- [ ] Code generation
  - [x] Structs to hold query results
  - [x] Functions to execute query
  - [ ] Call into Python to run resolvers
    - [ ] Get pointers to Python functions callable by JIT-compiled code
  - [ ] Default resolver (for scalars without defined resolvers)
  - [ ] Lists
  - [ ] Handle nullability
  - [ ] Promises
  - [ ] Properly increment and decrement Python object refcounts
- [ ] Invoke compiled code from Python
- [ ] Error handling
- [ ] Error reporting

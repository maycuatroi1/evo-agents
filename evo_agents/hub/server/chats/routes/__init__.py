"""The routes layer of group chats: the HTTP routes under /v1/chats and /v1/me, the chat routes of the worker protocol
under /v1/worker and an author run's chat under /v1/projects. Each opens the transaction, passes the request's parts
to the service and logs what happened."""

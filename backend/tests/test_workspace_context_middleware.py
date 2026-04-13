from deerflow.agents.middlewares.workspace_context_middleware import WorkspaceContextMiddleware


def test_workspace_context_message_defaults_generic_code_requests_to_workspace():
    middleware = WorkspaceContextMiddleware()

    message = middleware._build_workspace_message(
        "/tmp/workspace",
        "/mnt/project",
        "/tmp/uploads",
        "/tmp/outputs",
    )

    content = str(message.content)
    assert "Preferred workspace tool path: `/mnt/project`" in content
    assert "materials already available in this thread" in content
    assert "Treat uploads as a secondary location" in content
    assert "supported PDFs and Office files can be read with `read_file`" in content
    assert "Spend a small exploration budget on cheap local evidence first" in content
    assert "Only ask the user for a file path or target after you have checked the current workspace" in content

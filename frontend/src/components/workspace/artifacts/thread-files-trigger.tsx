import { FolderTreeIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/workspace/tooltip";

import { useArtifacts } from "./context";

export const ThreadFilesTrigger = () => {
  const { open, panelTab, setOpen, setPanelTab } = useArtifacts();
  const isFilesPanelOpen = open && panelTab === "files";

  return (
    <Tooltip content="Show workspace files">
      <Button
        className="text-muted-foreground hover:text-foreground"
        variant="ghost"
        onClick={() => {
          if (isFilesPanelOpen) {
            setOpen(false);
            return;
          }
          setPanelTab("files");
          setOpen(true);
        }}
        aria-pressed={isFilesPanelOpen}
      >
        <FolderTreeIcon />
        Workspace
      </Button>
    </Tooltip>
  );
};

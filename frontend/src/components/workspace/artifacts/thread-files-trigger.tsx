import { FolderTreeIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/workspace/tooltip";

import { useArtifacts } from "./context";

export const ThreadFilesTrigger = () => {
  const { setOpen, setPanelTab } = useArtifacts();

  return (
    <Tooltip content="Show workspace files">
      <Button
        className="text-muted-foreground hover:text-foreground"
        variant="ghost"
        onClick={() => {
          setPanelTab("files");
          setOpen(true);
        }}
      >
        <FolderTreeIcon />
        Workspace
      </Button>
    </Tooltip>
  );
};

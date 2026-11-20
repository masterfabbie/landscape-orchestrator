// Operations → Custom operations: one app; the server says which page (boot.page).

import { mount } from "../lib/mount";
import "../landscape/landscape.css";
import "../activity/activity.css";
import "../templates/templates.css";
import "../lib/forms.css";
import { CustomOperationPage, CustomOperationsPage } from "./CustomOperations";

mount((boot) => {
  if (boot.page === "custom_new") return <CustomOperationPage id={null} />;
  if (boot.page === "custom") return <CustomOperationPage id={Number(boot.id)} />;
  return <CustomOperationsPage />;
});

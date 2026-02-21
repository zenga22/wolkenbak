"""
AWS S3 GUI Manager - wxPython application for managing S3 buckets and objects.

Features:
  - List / browse S3 buckets and objects
  - Upload and download files
  - View bucket / object metadata and properties
  - Generate pre-signed URLs with configurable expiry
  - AWS credential / region configuration panel
"""

import os
import threading
import datetime
import urllib.parse

import wx
import wx.adv
import wx.lib.mixins.listctrl as listmix
import boto3
from botocore.exceptions import ClientError, NoCredentialsError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human_size(num_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def run_in_thread(fn):
    """Decorator – run *fn* in a daemon thread so the GUI stays responsive."""
    def wrapper(*args, **kwargs):
        t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
        t.start()
    return wrapper


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class CredentialsDialog(wx.Dialog):
    """Dialog for entering / editing AWS credentials and region."""

    def __init__(self, parent, access_key="", secret_key="",
                 session_token="", region="us-east-1", endpoint_url=""):
        super().__init__(parent, title="AWS Credentials & Region",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)

        def row(label, value, style=0):
            grid.Add(wx.StaticText(self, label=label),
                     flag=wx.ALIGN_CENTER_VERTICAL)
            ctrl = wx.TextCtrl(self, value=value, style=style,
                               size=(340, -1))
            grid.Add(ctrl, flag=wx.EXPAND)
            return ctrl

        self.txt_access  = row("Access Key ID:",     access_key)
        self.txt_secret  = row("Secret Access Key:", secret_key,
                               wx.TE_PASSWORD)
        self.txt_token   = row("Session Token\n(optional):", session_token)
        self.txt_region  = row("Region:",            region)
        self.txt_endpoint = row("Endpoint URL\n(optional / MinIO):", endpoint_url)

        sizer.Add(grid, proportion=1, flag=wx.ALL | wx.EXPAND, border=12)

        note = wx.StaticText(
            self,
            label="Leave Access Key / Secret blank to use the default\n"
                  "credential chain (~/.aws, environment variables, etc.)."
        )
        note.SetForegroundColour(wx.Colour(80, 80, 80))
        sizer.Add(note, flag=wx.LEFT | wx.BOTTOM, border=12)

        btn_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(btn_sizer, flag=wx.ALL | wx.ALIGN_RIGHT, border=8)

        self.SetSizerAndFit(sizer)

    # -- accessors -----------------------------------------------------------

    def get_values(self):
        return {
            "access_key":   self.txt_access.GetValue().strip(),
            "secret_key":   self.txt_secret.GetValue().strip(),
            "session_token": self.txt_token.GetValue().strip(),
            "region":       self.txt_region.GetValue().strip() or "us-east-1",
            "endpoint_url": self.txt_endpoint.GetValue().strip() or None,
        }


class PresignDialog(wx.Dialog):
    """Dialog for generating a presigned (time-limited) S3 URL."""

    def __init__(self, parent, bucket, key):
        super().__init__(parent, title="Generate Pre-signed URL",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.bucket = bucket
        self.key    = key

        sizer = wx.BoxSizer(wx.VERTICAL)

        info = wx.StaticText(
            self,
            label=f"Object:  s3://{bucket}/{key}"
        )
        info.SetFont(info.GetFont().Bold())
        sizer.Add(info, flag=wx.ALL, border=10)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=6)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(self, label="Expiry (seconds):"),
                 flag=wx.ALIGN_CENTER_VERTICAL)
        self.spin_expiry = wx.SpinCtrl(self, value="3600", min=60,
                                       max=604800, size=(120, -1))
        grid.Add(self.spin_expiry)

        grid.Add(wx.StaticText(self, label="HTTP method:"),
                 flag=wx.ALIGN_CENTER_VERTICAL)
        self.choice_method = wx.Choice(self, choices=["GET", "PUT"])
        self.choice_method.SetSelection(0)
        grid.Add(self.choice_method)

        sizer.Add(grid, flag=wx.ALL | wx.EXPAND, border=10)

        btn_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindowById(wx.ID_OK).SetLabel("Generate URL")
        sizer.Add(btn_sizer, flag=wx.ALL | wx.ALIGN_RIGHT, border=8)

        self.SetSizerAndFit(sizer)

    def get_values(self):
        return {
            "expiry": self.spin_expiry.GetValue(),
            "method": self.choice_method.GetStringSelection(),
        }


class InfoDialog(wx.Dialog):
    """Shows a scrollable key/value table of S3 metadata."""

    def __init__(self, parent, title, data: dict):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        sizer = wx.BoxSizer(wx.VERTICAL)
        lc = wx.ListCtrl(self, style=wx.LC_REPORT | wx.BORDER_SUNKEN,
                         size=(560, 360))
        lc.InsertColumn(0, "Property", width=220)
        lc.InsertColumn(1, "Value",    width=320)

        for i, (k, v) in enumerate(data.items()):
            lc.InsertItem(i, str(k))
            lc.SetItem(i, 1, str(v))

        sizer.Add(lc, proportion=1, flag=wx.ALL | wx.EXPAND, border=10)
        btn = wx.Button(self, wx.ID_OK, "Close")
        sizer.Add(btn, flag=wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, border=10)
        self.SetSizerAndFit(sizer)


# ---------------------------------------------------------------------------
# Custom list controls
# ---------------------------------------------------------------------------

class BucketList(wx.ListCtrl, listmix.ListCtrlAutoWidthMixin):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.BORDER_SUNKEN
                         | wx.LC_SINGLE_SEL)
        listmix.ListCtrlAutoWidthMixin.__init__(self)
        self.InsertColumn(0, "Bucket Name",    width=260)
        self.InsertColumn(1, "Creation Date",  width=180)
        self.InsertColumn(2, "Region",         width=120)


class ObjectList(wx.ListCtrl, listmix.ListCtrlAutoWidthMixin):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
        listmix.ListCtrlAutoWidthMixin.__init__(self)
        self.InsertColumn(0, "Key",           width=340)
        self.InsertColumn(1, "Size",          width=90)
        self.InsertColumn(2, "Last Modified", width=180)
        self.InsertColumn(3, "Storage Class", width=100)
        self.InsertColumn(4, "ETag",          width=260)


# ---------------------------------------------------------------------------
# Main Frame
# ---------------------------------------------------------------------------

class S3ManagerFrame(wx.Frame):
    """Top-level application window."""

    # -- construction --------------------------------------------------------

    def __init__(self):
        super().__init__(None, title="AWS S3 Manager",
                         size=(1100, 720),
                         style=wx.DEFAULT_FRAME_STYLE)

        self._creds     = {}          # current credential settings
        self._s3_client = None        # boto3 S3 client
        self._current_bucket = None   # bucket currently displayed
        self._prefix    = ""          # current folder prefix in the bucket

        self._build_menu()
        self._build_ui()
        self._build_statusbar()

        self.Centre()
        self.Show()

    # -- menu ----------------------------------------------------------------

    def _build_menu(self):
        mb = wx.MenuBar()

        # File
        m_file = wx.Menu()
        m_file.Append(wx.ID_PREFERENCES, "&Credentials / Region\tCtrl+,",
                      "Edit AWS credentials")
        m_file.AppendSeparator()
        m_file.Append(wx.ID_EXIT, "E&xit\tCtrl+Q")
        mb.Append(m_file, "&File")

        # Buckets
        m_bucket = wx.Menu()
        self.ID_NEW_BUCKET   = wx.NewIdRef()
        self.ID_DELETE_BUCKET= wx.NewIdRef()
        self.ID_BUCKET_INFO  = wx.NewIdRef()
        self.ID_BUCKET_POLICY= wx.NewIdRef()
        m_bucket.Append(self.ID_NEW_BUCKET,    "&Create Bucket…")
        m_bucket.Append(self.ID_DELETE_BUCKET, "&Delete Bucket…")
        m_bucket.AppendSeparator()
        m_bucket.Append(self.ID_BUCKET_INFO,   "Bucket &Info / Tags")
        m_bucket.Append(self.ID_BUCKET_POLICY, "Bucket &ACL Info")
        mb.Append(m_bucket, "&Buckets")

        # Objects
        m_obj = wx.Menu()
        self.ID_UPLOAD      = wx.NewIdRef()
        self.ID_UPLOAD_DIR  = wx.NewIdRef()
        self.ID_DOWNLOAD    = wx.NewIdRef()
        self.ID_DELETE_OBJ  = wx.NewIdRef()
        self.ID_OBJ_INFO    = wx.NewIdRef()
        self.ID_PRESIGN     = wx.NewIdRef()
        self.ID_COPY_KEY    = wx.NewIdRef()
        m_obj.Append(self.ID_UPLOAD,     "&Upload File…\tCtrl+U")
        m_obj.Append(self.ID_UPLOAD_DIR, "Upload &Directory…")
        m_obj.AppendSeparator()
        m_obj.Append(self.ID_DOWNLOAD,   "&Download Selected…\tCtrl+D")
        m_obj.AppendSeparator()
        m_obj.Append(self.ID_DELETE_OBJ, "De&lete Selected…")
        m_obj.AppendSeparator()
        m_obj.Append(self.ID_OBJ_INFO,   "Object &Properties")
        m_obj.Append(self.ID_PRESIGN,    "&Pre-sign URL…\tCtrl+P")
        m_obj.Append(self.ID_COPY_KEY,   "&Copy S3 URI")
        mb.Append(m_obj, "&Objects")

        self.SetMenuBar(mb)

        # Events
        self.Bind(wx.EVT_MENU, self._on_credentials,    id=wx.ID_PREFERENCES)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(),  id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, self._on_create_bucket,  id=self.ID_NEW_BUCKET)
        self.Bind(wx.EVT_MENU, self._on_delete_bucket,  id=self.ID_DELETE_BUCKET)
        self.Bind(wx.EVT_MENU, self._on_bucket_info,    id=self.ID_BUCKET_INFO)
        self.Bind(wx.EVT_MENU, self._on_bucket_acl,     id=self.ID_BUCKET_POLICY)
        self.Bind(wx.EVT_MENU, self._on_upload,         id=self.ID_UPLOAD)
        self.Bind(wx.EVT_MENU, self._on_upload_dir,     id=self.ID_UPLOAD_DIR)
        self.Bind(wx.EVT_MENU, self._on_download,       id=self.ID_DOWNLOAD)
        self.Bind(wx.EVT_MENU, self._on_delete_objects, id=self.ID_DELETE_OBJ)
        self.Bind(wx.EVT_MENU, self._on_object_info,    id=self.ID_OBJ_INFO)
        self.Bind(wx.EVT_MENU, self._on_presign,        id=self.ID_PRESIGN)
        self.Bind(wx.EVT_MENU, self._on_copy_key,       id=self.ID_COPY_KEY)

    # -- UI ------------------------------------------------------------------

    def _build_ui(self):
        panel = wx.Panel(self)
        vbox  = wx.BoxSizer(wx.VERTICAL)

        # ── Top toolbar ─────────────────────────────────────────────────────
        tb = wx.ToolBar(panel, style=wx.TB_HORIZONTAL | wx.TB_TEXT
                        | wx.TB_FLAT | wx.TB_NODIVIDER)
        tb.SetToolBitmapSize((24, 24))

        def art(id_):
            return wx.ArtProvider.GetBitmap(id_, wx.ART_TOOLBAR, (24, 24))

        self.btn_connect = tb.AddTool(
            wx.ID_ANY, "Connect", art(wx.ART_TICK_MARK),
            shortHelp="Connect / refresh buckets")
        tb.AddSeparator()

        self.btn_upload = tb.AddTool(
            wx.ID_ANY, "Upload", art(wx.ART_GO_UP),
            shortHelp="Upload file to current bucket")
        self.btn_download = tb.AddTool(
            wx.ID_ANY, "Download", art(wx.ART_GO_DOWN),
            shortHelp="Download selected object")
        tb.AddSeparator()

        self.btn_delete = tb.AddTool(
            wx.ID_ANY, "Delete", art(wx.ART_DELETE),
            shortHelp="Delete selected objects")
        tb.AddSeparator()

        self.btn_presign = tb.AddTool(
            wx.ID_ANY, "Pre-sign", art(wx.ART_HELP_PAGE),
            shortHelp="Generate pre-signed URL")
        tb.AddSeparator()

        self.btn_creds = tb.AddTool(
            wx.ID_ANY, "Credentials", art(wx.ART_EXECUTABLE_FILE),
            shortHelp="Edit AWS credentials / region")

        tb.Realize()
        vbox.Add(tb, flag=wx.EXPAND)

        # ── Path bar ────────────────────────────────────────────────────────
        hpath = wx.BoxSizer(wx.HORIZONTAL)
        hpath.Add(wx.StaticText(panel, label=" Location: "),
                  flag=wx.ALIGN_CENTER_VERTICAL)
        self.txt_path = wx.TextCtrl(panel, style=wx.TE_READONLY)
        self.txt_path.SetBackgroundColour(wx.Colour(245, 245, 245))
        hpath.Add(self.txt_path, proportion=1, flag=wx.EXPAND | wx.RIGHT, border=4)
        self.btn_up = wx.Button(panel, label="^ Up", size=(52, -1))
        hpath.Add(self.btn_up, flag=wx.ALIGN_CENTER_VERTICAL)
        vbox.Add(hpath, flag=wx.EXPAND | wx.ALL, border=4)

        # ── Splitter: bucket list (left) | object list (right) ──────────────
        splitter = wx.SplitterWindow(panel, style=wx.SP_LIVE_UPDATE)

        # Left – bucket list
        left = wx.Panel(splitter)
        lv   = wx.BoxSizer(wx.VERTICAL)
        lv.Add(wx.StaticText(left, label="Buckets"), flag=wx.ALL, border=4)
        self.bucket_list = BucketList(left)
        lv.Add(self.bucket_list, proportion=1, flag=wx.EXPAND | wx.ALL, border=4)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_new_bucket = wx.Button(left, label="New…",    size=(60, -1))
        self.btn_del_bucket = wx.Button(left, label="Delete…", size=(60, -1))
        self.btn_refresh_b  = wx.Button(left, label="Refresh", size=(60, -1))
        btn_row.Add(self.btn_new_bucket, flag=wx.RIGHT, border=4)
        btn_row.Add(self.btn_del_bucket, flag=wx.RIGHT, border=4)
        btn_row.Add(self.btn_refresh_b)
        lv.Add(btn_row, flag=wx.ALL, border=4)
        left.SetSizer(lv)

        # Right – object list
        right = wx.Panel(splitter)
        rv    = wx.BoxSizer(wx.VERTICAL)

        # search / filter bar
        hfilt = wx.BoxSizer(wx.HORIZONTAL)
        hfilt.Add(wx.StaticText(right, label="Filter prefix:"),
                  flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=4)
        self.txt_filter = wx.TextCtrl(right, size=(220, -1))
        self.btn_filter = wx.Button(right, label="Go", size=(40, -1))
        self.btn_clear_filter = wx.Button(right, label="Clear", size=(50, -1))
        hfilt.Add(self.txt_filter,       flag=wx.RIGHT,              border=4)
        hfilt.Add(self.btn_filter,       flag=wx.RIGHT,              border=4)
        hfilt.Add(self.btn_clear_filter, flag=wx.ALIGN_CENTER_VERTICAL)
        rv.Add(hfilt, flag=wx.ALL, border=4)

        self.obj_list = ObjectList(right)
        rv.Add(self.obj_list, proportion=1, flag=wx.EXPAND | wx.ALL, border=4)

        # object action buttons
        btn_row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_upload_f   = wx.Button(right, label="Upload File…",   size=(110,-1))
        self.btn_upload_d   = wx.Button(right, label="Upload Dir…",    size=(110,-1))
        self.btn_download_f = wx.Button(right, label="Download",       size=(90, -1))
        self.btn_delete_o   = wx.Button(right, label="Delete",         size=(70, -1))
        self.btn_info_o     = wx.Button(right, label="Properties",     size=(90, -1))
        self.btn_presign_o  = wx.Button(right, label="Pre-sign URL…",  size=(110,-1))
        for b in (self.btn_upload_f, self.btn_upload_d, self.btn_download_f,
                  self.btn_delete_o, self.btn_info_o, self.btn_presign_o):
            btn_row2.Add(b, flag=wx.RIGHT, border=4)
        rv.Add(btn_row2, flag=wx.ALL, border=4)
        right.SetSizer(rv)

        splitter.SplitVertically(left, right, 300)
        splitter.SetMinimumPaneSize(180)
        vbox.Add(splitter, proportion=1, flag=wx.EXPAND | wx.ALL, border=4)

        # ── Log panel ───────────────────────────────────────────────────────
        self.log = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY
                               | wx.TE_RICH2 | wx.HSCROLL,
                               size=(-1, 110))
        self.log.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE,
                                 wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        vbox.Add(self.log, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                 border=4)

        panel.SetSizer(vbox)

        # ── Event bindings ───────────────────────────────────────────────────
        self.Bind(wx.EVT_TOOL, self._on_connect,       self.btn_connect)
        self.Bind(wx.EVT_TOOL, self._on_upload,        self.btn_upload)
        self.Bind(wx.EVT_TOOL, self._on_download,      self.btn_download)
        self.Bind(wx.EVT_TOOL, self._on_delete_objects,self.btn_delete)
        self.Bind(wx.EVT_TOOL, self._on_presign,       self.btn_presign)
        self.Bind(wx.EVT_TOOL, self._on_credentials,   self.btn_creds)

        self.bucket_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_bucket_select)
        self.obj_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED,    self._on_obj_dbl_click)
        self.obj_list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK,  self._on_obj_right_click)

        self.btn_new_bucket.Bind(wx.EVT_BUTTON, self._on_create_bucket)
        self.btn_del_bucket.Bind(wx.EVT_BUTTON, self._on_delete_bucket)
        self.btn_refresh_b.Bind(wx.EVT_BUTTON,  lambda e: self._refresh_buckets())
        self.btn_up.Bind(wx.EVT_BUTTON,         self._on_navigate_up)
        self.btn_filter.Bind(wx.EVT_BUTTON,     self._on_apply_filter)
        self.btn_clear_filter.Bind(wx.EVT_BUTTON, self._on_clear_filter)
        self.txt_filter.Bind(wx.EVT_TEXT_ENTER, self._on_apply_filter)

        self.btn_upload_f.Bind(wx.EVT_BUTTON,  self._on_upload)
        self.btn_upload_d.Bind(wx.EVT_BUTTON,  self._on_upload_dir)
        self.btn_download_f.Bind(wx.EVT_BUTTON,self._on_download)
        self.btn_delete_o.Bind(wx.EVT_BUTTON,  self._on_delete_objects)
        self.btn_info_o.Bind(wx.EVT_BUTTON,    self._on_object_info)
        self.btn_presign_o.Bind(wx.EVT_BUTTON, self._on_presign)

    def _build_statusbar(self):
        self.sb = self.CreateStatusBar(3)
        self.sb.SetStatusWidths([-1, 220, 120])
        self.sb.SetStatusText("Not connected", 0)
        self.sb.SetStatusText("", 1)
        self.sb.SetStatusText("", 2)

    # -- Logging -------------------------------------------------------------

    def log_msg(self, msg: str, colour=None):
        """Append a log line (thread-safe via CallAfter)."""
        def _do():
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log.SetDefaultStyle(wx.TextAttr(colour or wx.NullColour))
            self.log.AppendText(f"[{ts}] {msg}\n")
        wx.CallAfter(_do)

    def log_ok(self, msg):   self.log_msg("✔ " + msg, wx.Colour(0, 120, 0))
    def log_err(self, msg):  self.log_msg("✘ " + msg, wx.Colour(180, 0, 0))
    def log_info(self, msg): self.log_msg("ℹ " + msg)

    def set_status(self, text, col=0):
        wx.CallAfter(self.sb.SetStatusText, text, col)

    # -- S3 client -----------------------------------------------------------

    def _make_client(self):
        """Build (or rebuild) the boto3 S3 client from stored credentials."""
        kw = dict(region_name=self._creds.get("region", "us-east-1"))
        ak = self._creds.get("access_key", "")
        sk = self._creds.get("secret_key", "")
        if ak and sk:
            kw["aws_access_key_id"]     = ak
            kw["aws_secret_access_key"] = sk
            st = self._creds.get("session_token", "")
            if st:
                kw["aws_session_token"] = st
        ep = self._creds.get("endpoint_url")
        if ep:
            kw["endpoint_url"] = ep
        self._s3_client = boto3.client("s3", **kw)
        return self._s3_client

    def _require_client(self) -> bool:
        """Ensure we have a client; prompt for credentials if not."""
        if self._s3_client is None:
            self._on_credentials(None)
        return self._s3_client is not None

    def _require_bucket(self) -> bool:
        if not self._current_bucket:
            wx.MessageBox("Please select a bucket first.",
                          "No Bucket Selected", wx.OK | wx.ICON_INFORMATION)
            return False
        return True

    # -- Credentials / connect -----------------------------------------------

    def _on_credentials(self, _event):
        dlg = CredentialsDialog(
            self,
            access_key=self._creds.get("access_key", ""),
            secret_key=self._creds.get("secret_key", ""),
            session_token=self._creds.get("session_token", ""),
            region=self._creds.get("region", "us-east-1"),
            endpoint_url=self._creds.get("endpoint_url", ""),
        )
        if dlg.ShowModal() == wx.ID_OK:
            self._creds = dlg.get_values()
            self._s3_client = None   # force rebuild
            self.log_info("Credentials updated.")
        dlg.Destroy()

    def _on_connect(self, _event):
        if not self._require_client():
            return
        self._refresh_buckets()

    # -- Bucket listing ------------------------------------------------------

    def _refresh_buckets(self):
        if not self._require_client():
            return
        self.log_info("Listing buckets…")
        self.set_status("Listing buckets…")

        @run_in_thread
        def _worker():
            try:
                resp = self._s3_client.list_buckets()
                buckets = resp.get("Buckets", [])
                owner   = resp.get("Owner", {}).get("DisplayName", "")

                def _ui():
                    self.bucket_list.DeleteAllItems()
                    for i, b in enumerate(buckets):
                        name    = b.get("Name", "")
                        created = str(b.get("CreationDate", ""))
                        # try to get bucket region
                        try:
                            loc = self._s3_client.get_bucket_location(Bucket=name)
                            region = loc.get("LocationConstraint") or "us-east-1"
                        except Exception:
                            region = "unknown"
                        self.bucket_list.InsertItem(i, name)
                        self.bucket_list.SetItem(i, 1, created[:19])
                        self.bucket_list.SetItem(i, 2, region)

                    count = len(buckets)
                    self.set_status(
                        f"Connected  —  {count} bucket{'s' if count != 1 else ''}",
                        0)
                    self.set_status(f"Owner: {owner}", 1)
                    self.log_ok(f"Found {count} bucket(s).")

                wx.CallAfter(_ui)
            except (NoCredentialsError, ClientError) as exc:
                self.log_err(f"List buckets failed: {exc}")
                self.set_status("Error – check credentials / region", 0)

        _worker()

    def _on_bucket_select(self, event):
        idx = event.GetIndex()
        name = self.bucket_list.GetItemText(idx)
        self._current_bucket = name
        self._prefix         = ""
        self.txt_filter.SetValue("")
        self._refresh_objects()

    # -- Object listing ------------------------------------------------------

    def _refresh_objects(self, prefix: str = ""):
        if not self._require_client() or not self._current_bucket:
            return
        self._prefix = prefix
        display = f"s3://{self._current_bucket}/{prefix}"
        wx.CallAfter(self.txt_path.SetValue, display)
        self.log_info(f"Listing {display} …")
        self.set_status(f"Listing {display}…")

        @run_in_thread
        def _worker():
            try:
                paginator = self._s3_client.get_paginator("list_objects_v2")
                pages = paginator.paginate(
                    Bucket=self._current_bucket,
                    Prefix=prefix,
                    Delimiter="/"
                )

                folders = []
                objects = []
                for page in pages:
                    for cp in page.get("CommonPrefixes") or []:
                        folders.append(cp["Prefix"])
                    for obj in page.get("Contents") or []:
                        objects.append(obj)

                def _ui():
                    self.obj_list.DeleteAllItems()
                    row = 0
                    # show "folders" first
                    for f in sorted(folders):
                        display_name = f[len(prefix):].rstrip("/")
                        self.obj_list.InsertItem(row, f"[{display_name}/]")
                        self.obj_list.SetItem(row, 1, "--")
                        self.obj_list.SetItem(row, 2, "--")
                        self.obj_list.SetItem(row, 3, "FOLDER")
                        row += 1
                    # then objects (skip the "folder" key itself)
                    for obj in sorted(objects, key=lambda o: o["Key"]):
                        key = obj["Key"]
                        if key == prefix:
                            continue   # skip directory marker
                        display_key = key[len(prefix):]
                        size   = human_size(obj.get("Size", 0))
                        mtime  = str(obj.get("LastModified", ""))[:19]
                        sc     = obj.get("StorageClass", "STANDARD")
                        etag   = obj.get("ETag", "").strip('"')
                        self.obj_list.InsertItem(row, display_key)
                        self.obj_list.SetItem(row, 1, size)
                        self.obj_list.SetItem(row, 2, mtime)
                        self.obj_list.SetItem(row, 3, sc)
                        self.obj_list.SetItem(row, 4, etag)
                        row += 1

                    total = len(folders) + len(objects)
                    self.set_status(
                        f"{self._current_bucket}  —  "
                        f"{len(folders)} folder(s), {len(objects)} object(s)",
                        0)
                    self.log_ok(f"Listed {total} items.")

                wx.CallAfter(_ui)
            except ClientError as exc:
                self.log_err(f"List objects failed: {exc}")

        _worker()

    def _on_obj_dbl_click(self, event):
        """Navigate into a folder on double-click."""
        idx  = event.GetIndex()
        text = self.obj_list.GetItemText(idx)
        if text.startswith("[") and text.endswith("/]"):
            # it is a virtual folder
            folder_name = text[1:-1]  # strip [ and ]
            new_prefix = self._prefix + folder_name
            self._refresh_objects(new_prefix)
        # else: ignore double-click on files (could trigger download here)

    def _on_navigate_up(self, _event):
        if not self._prefix:
            return
        # strip trailing slash, then go up one level
        parts = self._prefix.rstrip("/").rsplit("/", 1)
        new_prefix = parts[0] + "/" if len(parts) > 1 else ""
        self._refresh_objects(new_prefix)

    def _on_apply_filter(self, _event):
        prefix = self.txt_filter.GetValue().strip()
        # prepend current folder prefix so filter acts within current folder
        self._refresh_objects(self._prefix + prefix)

    def _on_clear_filter(self, _event):
        self.txt_filter.SetValue("")
        self._refresh_objects(self._prefix)

    # -- Right-click context menu --------------------------------------------

    def _on_obj_right_click(self, event):
        menu = wx.Menu()
        menu.Append(self.ID_DOWNLOAD,  "Download…")
        menu.Append(self.ID_DELETE_OBJ,"Delete…")
        menu.AppendSeparator()
        menu.Append(self.ID_OBJ_INFO,  "Properties")
        menu.Append(self.ID_PRESIGN,   "Pre-sign URL…")
        menu.Append(self.ID_COPY_KEY,  "Copy S3 URI")
        self.PopupMenu(menu)
        menu.Destroy()

    # -- Bucket operations ---------------------------------------------------

    def _on_create_bucket(self, _event):
        if not self._require_client():
            return
        name = wx.GetTextFromUser("Bucket name:", "Create Bucket", "",
                                  parent=self)
        if not name:
            return
        region = self._creds.get("region", "us-east-1")
        self.log_info(f"Creating bucket '{name}' in {region}…")

        @run_in_thread
        def _worker():
            try:
                kw = {"Bucket": name}
                if region != "us-east-1":
                    kw["CreateBucketConfiguration"] = {
                        "LocationConstraint": region
                    }
                self._s3_client.create_bucket(**kw)
                self.log_ok(f"Bucket '{name}' created.")
                wx.CallAfter(self._refresh_buckets)
            except ClientError as exc:
                self.log_err(f"Create bucket failed: {exc}")

        _worker()

    def _on_delete_bucket(self, _event):
        if not self._require_client():
            return
        idx = self.bucket_list.GetFirstSelected()
        if idx == -1:
            wx.MessageBox("Select a bucket first.", "Delete Bucket",
                          wx.OK | wx.ICON_INFORMATION)
            return
        name = self.bucket_list.GetItemText(idx)
        if wx.MessageBox(
                f"Delete bucket '{name}'?\n\nThe bucket must be empty.",
                "Confirm Delete", wx.YES_NO | wx.ICON_WARNING) != wx.YES:
            return

        @run_in_thread
        def _worker():
            try:
                self._s3_client.delete_bucket(Bucket=name)
                self.log_ok(f"Bucket '{name}' deleted.")
                wx.CallAfter(self._refresh_buckets)
            except ClientError as exc:
                self.log_err(f"Delete bucket failed: {exc}")

        _worker()

    def _on_bucket_info(self, _event):
        if not self._require_client():
            return
        idx = self.bucket_list.GetFirstSelected()
        if idx == -1:
            wx.MessageBox("Select a bucket first.", "Bucket Info",
                          wx.OK | wx.ICON_INFORMATION)
            return
        name = self.bucket_list.GetItemText(idx)
        self.log_info(f"Fetching info for bucket '{name}'…")

        @run_in_thread
        def _worker():
            data = {}
            try:
                # Location
                loc = self._s3_client.get_bucket_location(Bucket=name)
                data["Region"] = loc.get("LocationConstraint") or "us-east-1"
            except Exception as exc:
                data["Region"] = f"Error: {exc}"
            try:
                # Versioning
                ver = self._s3_client.get_bucket_versioning(Bucket=name)
                data["Versioning"] = ver.get("Status", "Disabled")
            except Exception as exc:
                data["Versioning"] = f"Error: {exc}"
            try:
                # Tags
                tags = self._s3_client.get_bucket_tagging(Bucket=name)
                for t in tags.get("TagSet", []):
                    data[f"Tag: {t['Key']}"] = t["Value"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NoSuchTagSet":
                    data["Tags"] = f"Error: {exc}"
            try:
                # Encryption
                enc = self._s3_client.get_bucket_encryption(Bucket=name)
                rules = enc["ServerSideEncryptionConfiguration"]["Rules"]
                data["Encryption"] = str(
                    rules[0]["ApplyServerSideEncryptionByDefault"]
                    .get("SSEAlgorithm", "None"))
            except Exception:
                data["Encryption"] = "None / inaccessible"
            try:
                # Logging
                log = self._s3_client.get_bucket_logging(Bucket=name)
                data["Logging"] = str(log.get("LoggingEnabled", "Disabled"))
            except Exception as exc:
                data["Logging"] = f"Error: {exc}"

            wx.CallAfter(
                lambda: InfoDialog(self, f"Bucket Info: {name}", data
                                   ).ShowModal()
            )

        _worker()

    def _on_bucket_acl(self, _event):
        if not self._require_client():
            return
        idx = self.bucket_list.GetFirstSelected()
        if idx == -1:
            wx.MessageBox("Select a bucket first.", "Bucket ACL",
                          wx.OK | wx.ICON_INFORMATION)
            return
        name = self.bucket_list.GetItemText(idx)

        @run_in_thread
        def _worker():
            try:
                acl  = self._s3_client.get_bucket_acl(Bucket=name)
                data = {}
                data["Owner"] = (acl.get("Owner", {}).get("DisplayName")
                                 or acl.get("Owner", {}).get("ID", ""))
                for i, grant in enumerate(acl.get("Grants", []), 1):
                    grantee = grant.get("Grantee", {})
                    perm    = grant.get("Permission", "")
                    who = (grantee.get("DisplayName")
                           or grantee.get("URI", "")
                           or grantee.get("ID", ""))
                    data[f"Grant {i}"] = f"{who}  →  {perm}"
                wx.CallAfter(
                    lambda: InfoDialog(self, f"ACL: {name}", data).ShowModal()
                )
            except ClientError as exc:
                self.log_err(f"ACL fetch failed: {exc}")

        _worker()

    # -- Upload --------------------------------------------------------------

    def _on_upload(self, _event):
        if not self._require_client() or not self._require_bucket():
            return

        with wx.FileDialog(self, "Upload File", style=wx.FD_OPEN
                           | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            paths = dlg.GetPaths()

        for local_path in paths:
            filename = os.path.basename(local_path)
            key      = self._prefix + filename
            self._upload_file(local_path, key)

    def _on_upload_dir(self, _event):
        if not self._require_client() or not self._require_bucket():
            return

        with wx.DirDialog(self, "Select Directory to Upload",
                          style=wx.DD_DEFAULT_STYLE
                          | wx.DD_DIR_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            dir_path = dlg.GetPath()

        base = os.path.dirname(dir_path)
        for root, _dirs, files in os.walk(dir_path):
            for fname in files:
                full = os.path.join(root, fname)
                rel  = os.path.relpath(full, base).replace("\\", "/")
                key  = self._prefix + rel
                self._upload_file(full, key)

    @run_in_thread
    def _upload_file(self, local_path: str, key: str):
        size = os.path.getsize(local_path)
        self.log_info(f"Uploading {local_path}  →  "
                      f"s3://{self._current_bucket}/{key}  "
                      f"({human_size(size)})")
        self.set_status(f"Uploading {os.path.basename(local_path)}…")
        try:
            self._s3_client.upload_file(local_path, self._current_bucket, key)
            self.log_ok(f"Uploaded: {key}")
            self.set_status("Upload complete.")
            wx.CallAfter(self._refresh_objects, self._prefix)
        except ClientError as exc:
            self.log_err(f"Upload failed: {exc}")
            self.set_status("Upload failed.")

    # -- Download ------------------------------------------------------------

    def _on_download(self, _event):
        if not self._require_client() or not self._require_bucket():
            return

        selected = self._get_selected_keys()
        if not selected:
            wx.MessageBox("Select one or more objects to download.",
                          "Download", wx.OK | wx.ICON_INFORMATION)
            return

        # destination directory
        with wx.DirDialog(self, "Save to Folder",
                          style=wx.DD_DEFAULT_STYLE) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            dest_dir = dlg.GetPath()

        for key in selected:
            self._download_file(key, dest_dir)

    @run_in_thread
    def _download_file(self, key: str, dest_dir: str):
        filename = os.path.basename(key) or key.replace("/", "_")
        dest     = os.path.join(dest_dir, filename)
        self.log_info(f"Downloading s3://{self._current_bucket}/{key}  →  {dest}")
        self.set_status(f"Downloading {filename}…")
        try:
            self._s3_client.download_file(self._current_bucket, key, dest)
            self.log_ok(f"Downloaded: {dest}")
            self.set_status("Download complete.")
        except ClientError as exc:
            self.log_err(f"Download failed [{key}]: {exc}")
            self.set_status("Download failed.")

    # -- Delete objects -------------------------------------------------------

    def _on_delete_objects(self, _event):
        if not self._require_client() or not self._require_bucket():
            return
        keys = self._get_selected_keys()
        if not keys:
            wx.MessageBox("Select object(s) to delete.",
                          "Delete", wx.OK | wx.ICON_INFORMATION)
            return
        msg = f"Delete {len(keys)} object(s)?\n\n" + "\n".join(keys[:8])
        if len(keys) > 8:
            msg += f"\n… and {len(keys)-8} more"
        if wx.MessageBox(msg, "Confirm Delete",
                         wx.YES_NO | wx.ICON_WARNING) != wx.YES:
            return

        @run_in_thread
        def _worker():
            try:
                objs = [{"Key": k} for k in keys]
                resp = self._s3_client.delete_objects(
                    Bucket=self._current_bucket,
                    Delete={"Objects": objs, "Quiet": False}
                )
                deleted = len(resp.get("Deleted", []))
                errors  = resp.get("Errors", [])
                self.log_ok(f"Deleted {deleted} object(s).")
                for e in errors:
                    self.log_err(f"Delete error [{e['Key']}]: {e['Message']}")
                wx.CallAfter(self._refresh_objects, self._prefix)
            except ClientError as exc:
                self.log_err(f"Delete failed: {exc}")

        _worker()

    # -- Object properties ---------------------------------------------------

    def _on_object_info(self, _event):
        if not self._require_client() or not self._require_bucket():
            return
        keys = self._get_selected_keys()
        if not keys:
            wx.MessageBox("Select an object.", "Properties",
                          wx.OK | wx.ICON_INFORMATION)
            return
        key = keys[0]
        self.log_info(f"Fetching metadata for {key}…")

        @run_in_thread
        def _worker():
            try:
                head = self._s3_client.head_object(
                    Bucket=self._current_bucket, Key=key)
                data = {
                    "Key":              key,
                    "Bucket":           self._current_bucket,
                    "Size":             human_size(head.get("ContentLength", 0)),
                    "Size (bytes)":     head.get("ContentLength", 0),
                    "Last Modified":    str(head.get("LastModified", "")),
                    "ETag":             head.get("ETag", "").strip('"'),
                    "Content-Type":     head.get("ContentType", ""),
                    "Storage Class":    head.get("StorageClass", "STANDARD"),
                    "Server-side Enc.": head.get("ServerSideEncryption", "None"),
                    "Version ID":       head.get("VersionId", "N/A"),
                }
                # user-defined metadata
                for k, v in (head.get("Metadata") or {}).items():
                    data[f"x-amz-meta-{k}"] = v
                wx.CallAfter(
                    lambda: InfoDialog(self,
                                       f"Properties: {os.path.basename(key)}",
                                       data).ShowModal()
                )
            except ClientError as exc:
                self.log_err(f"Head object failed: {exc}")

        _worker()

    # -- Pre-signed URL -------------------------------------------------------

    def _on_presign(self, _event):
        if not self._require_client() or not self._require_bucket():
            return
        keys = self._get_selected_keys()
        if not keys:
            wx.MessageBox("Select an object to pre-sign.",
                          "Pre-sign URL", wx.OK | wx.ICON_INFORMATION)
            return
        key = keys[0]

        dlg = PresignDialog(self, self._current_bucket, key)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        vals = dlg.get_values()
        dlg.Destroy()

        try:
            url = self._s3_client.generate_presigned_url(
                ClientMethod="get_object" if vals["method"] == "GET"
                             else "put_object",
                Params={"Bucket": self._current_bucket, "Key": key},
                ExpiresIn=vals["expiry"],
            )
        except ClientError as exc:
            self.log_err(f"Pre-sign failed: {exc}")
            return

        expiry_dt = (datetime.datetime.utcnow()
                     + datetime.timedelta(seconds=vals["expiry"]))
        self.log_ok(
            f"Pre-signed URL ({vals['method']}, expires "
            f"{expiry_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC):\n  {url}"
        )

        # Show in a copyable dialog
        result_dlg = wx.Dialog(self, title="Pre-signed URL",
                               style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
                               size=(700, 280))
        sv = wx.BoxSizer(wx.VERTICAL)
        sv.Add(wx.StaticText(
            result_dlg,
            label=f"Method: {vals['method']}   "
                  f"Expires in: {vals['expiry']}s  "
                  f"({expiry_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC)"
        ), flag=wx.ALL, border=8)
        txt = wx.TextCtrl(result_dlg, value=url,
                          style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
                          size=(-1, 120))
        sv.Add(txt, proportion=1, flag=wx.EXPAND | wx.ALL, border=8)
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_copy  = wx.Button(result_dlg, label="Copy URL")
        btn_close = wx.Button(result_dlg, wx.ID_OK, label="Close")
        btn_row.Add(btn_copy,  flag=wx.RIGHT, border=8)
        btn_row.Add(btn_close)
        sv.Add(btn_row, flag=wx.ALIGN_RIGHT | wx.ALL, border=8)
        result_dlg.SetSizer(sv)

        def _copy(_e):
            if wx.TheClipboard.Open():
                wx.TheClipboard.SetData(wx.TextDataObject(url))
                wx.TheClipboard.Close()
            self.log_info("URL copied to clipboard.")

        btn_copy.Bind(wx.EVT_BUTTON, _copy)
        result_dlg.ShowModal()
        result_dlg.Destroy()

    # -- Copy S3 URI ---------------------------------------------------------

    def _on_copy_key(self, _event):
        if not self._require_bucket():
            return
        keys = self._get_selected_keys()
        if not keys:
            return
        uri = f"s3://{self._current_bucket}/{keys[0]}"
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(uri))
            wx.TheClipboard.Close()
        self.log_info(f"Copied: {uri}")

    # -- Helpers -------------------------------------------------------------

    def _get_selected_keys(self) -> list[str]:
        """Return the full S3 keys of all selected objects (not folders)."""
        keys  = []
        index = self.obj_list.GetFirstSelected()
        while index != -1:
            text = self.obj_list.GetItemText(index)
            if not (text.startswith("[") and text.endswith("/]")):
                keys.append(self._prefix + text)
            index = self.obj_list.GetNextSelected(index)
        return keys


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = wx.App(False)
    S3ManagerFrame()
    app.MainLoop()


if __name__ == "__main__":
    main()

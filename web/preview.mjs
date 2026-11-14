// Use the server's name; stale responses must never label another video/note.
export function createDestinationPreview({getDraftId, getNote, show, request,
  schedule = setTimeout, unschedule = clearTimeout, delay = 200}) {
  let version = 0, timer;
  function cancel() {
    version++;
    unschedule(timer);
  }
  function update() {
    cancel();
    const id = getDraftId(), note = getNote(), ticket = version;
    if (!id) return;
    if (!note.trim()) { show('写好点评与 #类目后显示'); return; }
    show('正在确认文件名…');
    const current = () => ticket === version && getDraftId() === id && getNote() === note;
    timer = schedule(async () => {
      try {
        const result = await request(note);
        if (current()) show(`${result.category} / ${result.filename}`, result.adjusted);
      } catch (error) {
        if (current()) show(error.message);
      }
    }, delay);
  }
  return {update, cancel};
}

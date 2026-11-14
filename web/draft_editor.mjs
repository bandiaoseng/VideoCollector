export const noteClosed = server => !!server?.task_id || ['cancelled','cancel_requested'].includes(server?.state);

export class NoteEditor {
  constructor(server, cached) {
    this.id=server.id; this.version=server.version; this.baseNote=server.note;this.server=server;
    this.note=cached?.note ?? server.note; this.conflict=null;
    if(cached && this.note!==server.note && cached.version!==server.version)this.conflict=server;
  }
  accept(server) {
    if(server.version<this.version || server.version<(this.conflict?.version??-1))return;
    this.version=server.version; this.baseNote=server.note; this.server=server;this.conflict=null;
  }
  observe(server) {
    if(server.version<this.server.version)return;
    this.server=server;
    if(noteClosed(server) && this.note!==server.note){this.conflict=server;return;}
    if(server.version<=this.version)return;
    if(this.note===this.baseNote || this.note===server.note){this.note=server.note;this.accept(server);}
    else this.conflict=server;
  }
  resolve(useLocal) {
    const remote=this.conflict;
    if(!remote)return;
    if(useLocal && noteClosed(remote))throw new Error('这条视频已经提交或取消，不能再替换点评');
    if(!useLocal)this.note=remote.note;
    this.accept(remote);
  }
}

export class DraftCache {
  constructor(storage, owner){this.storage=storage;this.owner=owner;}
  copies(id) {
    const prefix='collector.note.'+id;
    return [...new Set([prefix,...this.storage.keys().filter(key=>typeof key==='string'&&key.startsWith(prefix+'.'))])].flatMap(key=>{
      const raw=this.storage.get(key);if(raw===null)return [];
      let value;try{value=JSON.parse(raw);}catch{}
      if(!value || typeof value.note!=='string')value={note:raw,version:null,owner:'legacy'};
      return [{...value,key,raw}];
    });
  }
  current(id){return this.copies(id).find(copy=>copy.key==='collector.note.'+id);}
  save(editor) {
    const prefix='collector.note.'+editor.id;
    const previous=this.current(editor.id);
    if(previous&&previous.owner!==this.owner)this.storage.set(prefix+'.'+(previous.owner||'legacy'),previous.raw);
    const value=JSON.stringify({note:editor.note,version:editor.version,owner:this.owner});
    // Each page owns a separate copy, so another tab cannot erase its offline text.
    this.storage.set(prefix+'.'+this.owner,value);
    this.storage.set(prefix,value);
  }
  clear(editor) {
    for(const copy of this.copies(editor.id)){
      if(copy.owner===this.owner && this.storage.get(copy.key)===copy.raw)this.storage.remove(copy.key);
    }
  }
}

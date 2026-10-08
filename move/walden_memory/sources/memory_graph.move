module walden_memory::memory_graph {
    use sui::event;
    use sui::object::{Self, ID, UID};
    use sui::transfer;
    use sui::tx_context::{Self, TxContext};

    const E_STALE_REVISION: u64 = 1;

    public struct Entity has key, store {
        id: UID,
        kind: u8,             // 1 person, 2 room
        key_hash: vector<u8>, // pseudonymous HMAC hex bytes; never raw MXID/room ID
        head_blob_id: vector<u8>,
        revision: u64,
    }

    public struct HeadUpdated has copy, drop {
        entity: ID,
        revision: u64,
    }

    entry fun create_entity(kind: u8, key_hash: vector<u8>, head_blob_id: vector<u8>, ctx: &mut TxContext) {
        let e = Entity { id: object::new(ctx), kind, key_hash, head_blob_id, revision: 1 };
        transfer::transfer(e, tx_context::sender(ctx));
    }

    entry fun update_head(entity: &mut Entity, expected_revision: u64, new_head_blob_id: vector<u8>) {
        assert!(entity.revision == expected_revision, E_STALE_REVISION);
        entity.head_blob_id = new_head_blob_id;
        entity.revision = entity.revision + 1;
        event::emit(HeadUpdated { entity: object::uid_to_inner(&entity.id), revision: entity.revision });
    }

    public fun kind(entity: &Entity): u8 { entity.kind }
    public fun revision(entity: &Entity): u64 { entity.revision }
    public fun head_blob_id(entity: &Entity): &vector<u8> { &entity.head_blob_id }
}
